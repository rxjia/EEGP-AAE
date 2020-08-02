#!/usr/bin/env python
# coding: utf-8
# This is a demo, given a LINEMOD image 0002.png and the mask-RCNN result(mask and 2D boundingbox), do 6D pose estimation for the lamp:
# First based on RGB, then use depth map to refine the z-direction, finally refine both rotation and translation.
#Prerequisite before generating the codebook:
#ckpt: under workspace_path/experiments/<experiment_name>/checkpoints_lambda250/checkpoints/ckpt-<num_iterations>-1
#Rendered imgs and edgemaps under reference rotations \bar_R(Generated by render_codebook.py) under the path: path_embedding_data
#image,depth map, mask
#mesh.ply under the path: path_model

import cv2
import os
import numpy as np
import tensorflow as tf
import sonnet as snt
import open3d as o3d
from pysixd_stuff.pysixd import inout
from est_utils import est_tra_w_tz,rectify_rot,depth_refinement,rotation_error_icp
obj_id=14
num_iterations=30000
LATENT_SPACE_SIZE = 128
NUM_FILTER = [128, 256, 512, 512]
KERNEL_SIZE_ENCODER = 5
STRIDES =[2, 2, 2, 2]
BATCH_NORM = False
image_size=128
embedding_dim = 128
num_embeddings = 92232
K_train =np.array([572.41140, 0, 325.26110, 0, 573.57043, 242.04899, 0, 0, 1]).reshape((3, 3))#Should be consistent with \bar_R images
Radius_render_train = 700

K_test = np.array([572.4114, 0.0, 325.2611, 0.0, 573.57043, 242.04899, 0.0, 0.0, 1.0]).reshape(3,3)
experiment_name='linemod_{:02d}_softmax_edge'.format(obj_id)
path_workspath='./ws/'
path_embedding_data='./embedding92232s/{:02d}'.format(obj_id)
path_model='./ws/meshes/obj_{:02d}.ply'.format(obj_id)
model_ply = inout.load_ply(path_model)
model_o3d = o3d.io.read_point_cloud(path_model)

#### Step 0: Load pose estimation network
class Encoder(snt.AbstractModule):
    def __init__(self, latent_space_size, num_filters, kernel_size, strides, batch_norm, name='encoder'):
        super(Encoder, self).__init__(name=name)
        self._latent_space_size = latent_space_size
        self._num_filters = num_filters
        self._kernel_size = kernel_size
        self._strides = strides
        self._batch_normalization = batch_norm

    @property
    def latent_space_size(self):
        return self._latent_space_size

    @property
    def encoder_layers(self):
        layers = []
        x = self._input
        layers.append(x)
        for filters, stride in zip(self._num_filters, self._strides):
            padding = 'same'
            x = tf.layers.conv2d(
                inputs=x,
                filters=filters,
                kernel_size=self._kernel_size,
                strides=stride,
                padding=padding,
                kernel_initializer=tf.contrib.layers.xavier_initializer_conv2d(),
                activation=tf.nn.relu,
            )
            if self._batch_normalization:
                x = tf.layers.batch_normalization(x, training=self._is_training)
            layers.append(x)
        return layers

    @property
    def encoder_out(self):
        x = self.encoder_layers[-1]
        encoder_out = tf.contrib.layers.flatten(x)

        return encoder_out

    @property
    def z(self):
        x = self.encoder_out
        # construct data
        z = tf.layers.dense(
            x,
            self._latent_space_size,
            activation=None,
            kernel_initializer=tf.contrib.layers.xavier_initializer(),
            name=None
        )
        return z

    def _build(self, x, is_training=False):
        self._input = x
        self._is_training = is_training
        return self.z


class VectorQuantizer(snt.AbstractModule):
    def __init__(self, embedding_dim, num_embeddings, name='vq_center'):
        super(VectorQuantizer, self).__init__(name=name)
        self._embedding_dim = embedding_dim
        self._num_embeddings = num_embeddings

        with self._enter_variable_scope():
            initializer = tf.uniform_unit_scaling_initializer()
            self._w = tf.get_variable('embedding', [embedding_dim, num_embeddings], initializer=initializer, trainable=True)

    def _build(self, inputs):
        input_shape = tf.shape(inputs)
        with tf.control_dependencies([
            tf.Assert(tf.equal(input_shape[-1], self._embedding_dim),[input_shape])]):
            flat_inputs = tf.reshape(inputs, [-1, self._embedding_dim])
            w = self.embeddings.read_value()

        distances = -tf.matmul(tf.nn.l2_normalize(flat_inputs, axis=1), tf.nn.l2_normalize(w, axis=0))
        encoding_indices = tf.argmax(- distances, 1)
        encoding_indices = tf.reshape(encoding_indices, tf.shape(inputs)[:-1])        
        return {'encoding_indices': encoding_indices, }

    @property
    def embeddings(self):
        return self._w


# Build modules.
graph_estpose=tf.Graph()
with graph_estpose.as_default():
    with tf.variable_scope(experiment_name):  # .split('_')[0]+'_'+experiment_name.split('_')[1]):
        I_x = tf.placeholder(tf.float32, shape=(None, image_size, image_size, 4))
        with tf.variable_scope('encoder'):
            encoder = Encoder(latent_space_size=LATENT_SPACE_SIZE,num_filters=NUM_FILTER,kernel_size=KERNEL_SIZE_ENCODER,strides=STRIDES,batch_norm=BATCH_NORM)
        z = encoder(I_x)
        network_vars = tf.trainable_variables()
        print(network_vars)
        vq_codebook = VectorQuantizer(embedding_dim=embedding_dim,num_embeddings=num_embeddings)

        # For evaluation, make sure is_training=False!
        with tf.variable_scope('validation'):
            nn_item = vq_codebook(z)

        # Bounding box informations for foreground model in pose template repository
        codebook_obj_bbs = np.load(os.path.join(path_embedding_data,'obj_bbs.npy'))
        codebook_rotations = np.load(os.path.join(path_embedding_data,'rot_infos.npz'))['rots']

    saver = tf.train.Saver(network_vars, save_relative_paths=False)
    embedding = tf.placeholder(tf.float32, shape=[embedding_dim, num_embeddings])
    embedding_assign_op = tf.assign(vq_codebook.embeddings, embedding)


gpu_options = tf.GPUOptions(allow_growth=True, per_process_gpu_memory_fraction=0.9)
config = tf.ConfigProto(gpu_options=gpu_options)
sess_estpose=tf.Session(graph=graph_estpose,config=config)
print('Step 0, Load Rotation Estimation Net')
with sess_estpose.as_default():
    with graph_estpose.as_default():
        saver.restore(sess_estpose, '{:s}/experiments/{:s}/checkpoints_lambda250/checkpoints/chkpt-{:d}'.format(path_workspath,experiment_name,num_iterations-1))
        arr_codebook = np.load(os.path.join(path_embedding_data,'edgeLambda250_codebook.npy'))
        sess_estpose.run(embedding_assign_op, {embedding: arr_codebook.T})



print('Step 0, Load RGB image')
image = cv2.imread('./demo_data/0002.png')
img_depth = inout.load_depth2('./demo_data/0002_depth.png')
depth_scale=1.0
img_depth=img_depth*depth_scale

print('Step 1, Load 2D detection and mask')
img_masks = np.load('./demo_data/0002_mask.npy')
for cc in range(3):
    image[:, :, cc] = np.where(img_masks[:, :, 0] == 1,image[:, :, cc], 0)
img_depth = np.where(img_masks[:, :, 0] == 1, img_depth, 0)


obj_bb=np.array([326, 54, 103, 151])
#obj_bb is the 2D bounding box on the image, with 4 elements: x,y,w,h; where (x,y) is the location of the left-top corner
#Thus center 2D location is (x+w/2, h+w/2)

verbose=False
if verbose:
    image_vis=image.copy()
    cv2.rectangle(image_vis,(obj_bb[0],obj_bb[1]),(obj_bb[0]+obj_bb[2],obj_bb[1]+obj_bb[3]),(255,0,0),2)
    cv2.imshow('img',image_vis)
    cv2.waitKey()

print('Step 2, Pose estimation')
with sess_estpose.as_default():
    with graph_estpose.as_default():
        img_bgr=image.copy()

        x,y,w,h=obj_bb
        H,W,_=img_bgr.shape
        size = int(np.maximum(h, w) * 1.2)
        left = int(np.max([x + w / 2 - size / 2, 0]))
        right = int(np.min([x + w / 2 + size / 2, W]))
        top = int(np.max([y + h / 2 - size / 2, 0]))
        bottom = int(np.min([y + h / 2 + size / 2, H]))

        crop = img_bgr[top:bottom, left:right].copy()
        crop_depth=img_depth[top:bottom,left:right].copy()

        query_bgr = cv2.resize(crop, (image_size,image_size))
        query_edge = np.expand_dims(cv2.Canny(query_bgr, 50, 150),2)
        query = np.expand_dims((np.concatenate((query_bgr, query_edge), axis=-1) /255.),0)

        idx=sess_estpose.run([nn_item],feed_dict={I_x:query})
        idx=idx[0]['encoding_indices'][0]
        est_rot_cb= codebook_rotations[idx] #This should be a 3x3 matrix, which indicates an initial model-to-camera rotation estimation

        if verbose:
            #path='../Edge-Network/embedding92232s/{:02d}/imgs/{:05d}.png'.format(obj_id,idx)
            #est_bgr=cv2.imread(path)
            cv2.imshow('bbox',query_bgr)
            cv2.imshow('bbox_depth',crop_depth)
            #cv2.imshow('estimated rotation',est_bgr)
            cv2.waitKey()

        K00_ratio = K_test[0, 0] / K_train[0, 0]
        K11_ratio = K_test[1, 1] / K_train[1, 1]

        mean_K_ratio = np.mean([K00_ratio, K11_ratio])

        render_bb = codebook_obj_bbs[idx].squeeze()
        est_bb = obj_bb.copy() #Same as obj_bb, thus est_bb=[x,y,w,h], is the 2D bounding box detected, where (x,y) is the 2D location of left-top corner
        #Center of the 2D bbox is (x_c,y_c)=(est_bb[0]+est_bb[2]/2,est_bb[1]+est_bb[3]/2)
        diag_bb_ratio = np.linalg.norm(np.float32(render_bb[2:])) / np.linalg.norm(np.float32(est_bb[2:]))

        mm_tz = diag_bb_ratio * mean_K_ratio * Radius_render_train

        center_obj_x_train = render_bb[0] + render_bb[2] / 2. - K_train[0, 2]
        center_obj_y_train = render_bb[1] + render_bb[3] / 2. - K_train[1, 2]

        center_obj_x_test = est_bb[0] + est_bb[2] / 2 - K_test[0, 2]
        center_obj_y_test = est_bb[1] + est_bb[3] / 2 - K_test[1, 2]



        est_tra = est_tra_w_tz(mm_tz,Radius_render_train,K_test,center_obj_x_test,center_obj_y_test,K_train,center_obj_x_train,center_obj_y_train)
        est_rot=rectify_rot(est_rot_cb,est_tra)

        max_mean_dist_factor=2.0
        #Refine the z-direction only.
        mm_tz,max_mean_dist=depth_refinement(crop_depth, model_ply,est_rot.astype(np.float32),est_tra.flatten(), K_test, (W, H), max_mean_dist_factor=max_mean_dist_factor)#5.0)

        est_tra = est_tra_w_tz(mm_tz,Radius_render_train,K_test,center_obj_x_test,center_obj_y_test, K_train,center_obj_x_train,center_obj_y_train)
        est_rot=rectify_rot(est_rot_cb,est_tra)

        #Further icp refinement for rotation and translation
        if True:
            est_rot,est_tra,_= rotation_error_icp(img_depth, model_o3d,  obj_bb, est_rot, est_tra.flatten(),K_test.copy(),
                                                width=W, height=H, max_mean_dist=max_mean_dist,
                                                max_mean_dist_factor=max_mean_dist_factor,
                                                regist_error_threshold=3.0, fitness_threshold=0.7)


print('Step 3: Write result')
print('Estimated rotation - model2cam, 3x3 rotation matrix:') ###3x3 rotation matrix
print(est_rot.astype(np.float32).reshape((3,3)))
print('Estimated translation - model2cam, in mm:') ###translation vector, with unit mm
print(est_tra.astype(np.float32).reshape((1,3)))
print('Detected 2D bounding box - on 2D image, (x_topleft,y_topleft,bbox_width,bbox_height:')
print(np.array(obj_bb).astype(np.int32).reshape((1,4))) ### 2D bounding box


path_result='./result.txt'
with open(path_result, 'w') as f:
    line_tpl=', '.join(['{:.8f}'] * 9) + '\n' + ', '.join(['{:.8f}'] * 3)
    Rt = est_rot.astype(np.float32).flatten().tolist() + est_tra.flatten().tolist()
    txt=line_tpl.format(*Rt)
    f.write(txt)

'''
Expected result:
if refine for both rotation and translation:
est_rot: [-0.20446137, 0.94286263, -0.26306966, 0.85182256, 0.03896938, -0.52237886, -0.48227987, -0.33089498, -0.81111938], 
est_tra: [90.49808699, -137.58854774, 849.52665827]
if refine only z-direction
est_rot: [-0.15618885, 0.96754020, -0.19867308, 0.86759037, 0.03824597, -0.49580660, -0.47211438, -0.24980631, -0.84540218], 
est_tra: [85.44441029, -146.03545470, 840.30747686]
'''