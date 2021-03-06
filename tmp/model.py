# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#You may obtain a copy of the License at
#
#    https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from absl import flags
import tensorflow as tf
import discriminator as disc
import generator as generator_module
import ops
import utils_in as utils


tfgan = tf.contrib.gan

flags.DEFINE_string(
    'data_dir', '/tmp/data',
    'Directory with Imagenet input data')
flags.DEFINE_float('discriminator_learning_rate', 0.0004,
                   'Learning rate of for adam. [0.0004]')
flags.DEFINE_float('generator_learning_rate', 0.0004,
                   'Learning rate of for adam. [0.0004]')
flags.DEFINE_float('beta1', 0.0, 'Momentum term of adam. [0.5]')
flags.DEFINE_integer('image_size', 128, 'The size of image to use '
                     '(will be center cropped) [128]')
flags.DEFINE_integer('image_width', 128,
                     'The width of the images presented to the model')
flags.DEFINE_integer('data_parallelism', 64, 'The number of objects to read at'
                     ' one time when loading input data. [64]')
flags.DEFINE_integer('z_dim', 128, 'Dimensionality of latent code z. [8192]')
flags.DEFINE_integer('gf_dim', 64, 'Dimensionality of gf. [64]')
flags.DEFINE_integer('df_dim', 64, 'Dimensionality of df. [64]')
flags.DEFINE_integer('number_classes', 1000,
                     'The number of classes in the dataset')
flags.DEFINE_string('generator_type', 'baseline', 'test or baseline')
flags.DEFINE_string('discriminator_type', 'baseline', 'test or baseline')


FLAGS = flags.FLAGS


def _get_d_real_loss(discriminator_on_data_logits):
  loss = tf.nn.relu(1.0 - discriminator_on_data_logits)
  return tf.reduce_mean(loss)


def _get_d_fake_loss(discriminator_on_generator_logits):
  return tf.reduce_mean(tf.nn.relu(1 + discriminator_on_generator_logits))


def _get_g_loss(discriminator_on_generator_logits):
  return -tf.reduce_mean(discriminator_on_generator_logits)


class SNGAN(object):

  def __init__(self, zs, config=None, global_step=None, devices=None):
    self.config = config
    self.image_size = FLAGS.image_size
    self.image_shape = [FLAGS.image_size, FLAGS.image_size, 3]
    self.z_dim = FLAGS.z_dim
    self.gf_dim = FLAGS.gf_dim
    self.df_dim = FLAGS.df_dim
    self.num_classes = FLAGS.number_classes

    self.data_parallelism = FLAGS.data_parallelism
    self.zs = zs

    self.c_dim = 3
    self.dataset_name = 'imagenet'
    self.devices = devices
    self.global_step = global_step

    self.build_model()

  def build_model(self):
    config = self.config
    self.d_opt = tf.train.AdamOptimizer(
        FLAGS.discriminator_learning_rate, beta1=FLAGS.beta1)
    self.g_opt = tf.train.AdamOptimizer(
        FLAGS.generator_learning_rate, beta1=FLAGS.beta1)

    with tf.variable_scope('model') as model_scope:
      if config.num_towers > 1:
        all_d_grads = []
        all_g_grads = []
        for idx, device in enumerate(self.devices):
          with tf.device('/%s' % device):
            with tf.name_scope('device_%s' % idx):
              with ops.variables_on_gpu0():
                self.build_model_single_gpu(
                    gpu_idx=idx,
                    batch_size=config.batch_size,
                    num_towers=config.num_towers)
                d_grads = self.d_opt.compute_gradients(self.d_losses[-1],
                                                       var_list=self.d_vars)
                g_grads = self.g_opt.compute_gradients(self.g_losses[-1],
                                                       var_list=self.g_vars)
                all_d_grads.append(d_grads)
                all_g_grads.append(g_grads)
                model_scope.reuse_variables()
        d_grads = ops.avg_grads(all_d_grads)
        g_grads = ops.avg_grads(all_g_grads)
      else:
        self.build_model_single_gpu(batch_size=config.batch_size,
                                    num_towers=config.num_towers)
        d_grads = self.d_opt.compute_gradients(self.d_losses[-1],
                                               var_list=self.d_vars)
        g_grads = self.g_opt.compute_gradients(self.g_losses[-1],
                                               var_list=self.g_vars)

    d_step = tf.get_variable('d_step', initializer=0, trainable=False)
    self.d_optim = self.d_opt.apply_gradients(d_grads, global_step=d_step)
    g_step = tf.get_variable('g_step', initializer=0, trainable=False)
    self.g_optim = self.g_opt.apply_gradients(g_grads, global_step=g_step)

  def build_model_single_gpu(self, gpu_idx=0, batch_size=1, num_towers=1):
    config = self.config
    show_num = min(config.batch_size, 64)

    reuse_vars = gpu_idx > 0
    if gpu_idx == 0:
      self.increment_global_step = self.global_step.assign_add(1)
      self.batches = utils.get_imagenet_batches(
          FLAGS.data_dir, batch_size, num_towers, label_offset=0,
          cycle_length=config.data_parallelism,
          shuffle_buffer_size=config.shuffle_buffer_size)
      sample_images, _ = self.batches[0]
      vis_images = tf.cast((sample_images + 1.) * 127.5, tf.uint8)
      tf.summary.image('input_image_grid',
                       tfgan.eval.image_grid(
                           vis_images[:show_num],
                           grid_shape=utils.squarest_grid_size(
                               show_num),
                           image_shape=(128, 128)))

    images, sparse_labels = self.batches[gpu_idx]
    sparse_labels = tf.squeeze(sparse_labels)

    gen_class_logits = tf.zeros((batch_size, self.num_classes))
    gen_class_ints = tf.multinomial(gen_class_logits, 1)
    gen_sparse_class = tf.squeeze(gen_class_ints)
    gen_class_ints = tf.squeeze(gen_class_ints)
    gen_class_vector = tf.one_hot(gen_class_ints, self.num_classes)

    if FLAGS.generator_type == 'baseline':
      generator_fn = generator_module.generator
    elif FLAGS.generator_type == 'test':
      generator_fn = generator_module.generator_test

    generator = generator_fn(
        self.zs[gpu_idx],
        gen_sparse_class,
        self.gf_dim,
        self.num_classes,
        reuse_vars=reuse_vars,
        )

    if gpu_idx == 0:
      generator_means = tf.reduce_mean(generator, 0, keep_dims=True)
      generator_vars = tf.reduce_mean(
          tf.squared_difference(generator, generator_means), 0, keep_dims=True)
      generator = tf.Print(
          generator,
          [tf.reduce_mean(generator_means), tf.reduce_mean(generator_vars)],
          'generator mean and average var', first_n=1)
      image_means = tf.reduce_mean(images, 0, keep_dims=True)
      image_vars = tf.reduce_mean(
          tf.squared_difference(images, image_means), 0, keep_dims=True)
      images = tf.Print(
          images, [tf.reduce_mean(image_means), tf.reduce_mean(image_vars)],
          'image mean and average var', first_n=1)
      sparse_labels = tf.Print(
          sparse_labels, [sparse_labels, sparse_labels.shape],
          'sparse_labels', first_n=2)
      gen_sparse_class = tf.Print(
          gen_sparse_class, [gen_sparse_class, gen_sparse_class.shape],
          'gen_sparse_labels', first_n=2)

      self.generators = []

    self.generators.append(generator)

    if FLAGS.discriminator_type == 'baseline':
      discriminator_fn = disc.discriminator
    elif FLAGS.discriminator_type == 'test':
      discriminator_fn = disc.discriminator_test
    else:
      raise NotImplementedError
    discriminator_on_data_logits = discriminator_fn(
        images, sparse_labels, self.df_dim, self.num_classes,
        reuse_vars=reuse_vars, update_collection=None)
    discriminator_on_generator_logits = discriminator_fn(
        generator, gen_sparse_class, self.df_dim, self.num_classes,
        reuse_vars=True, update_collection='NO_OPS')

    vis_generator = tf.cast((generator + 1.) * 127.5, tf.uint8)
    tf.summary.image('generator', vis_generator)

    tf.summary.image('generator_grid',
                     tfgan.eval.image_grid(
                         vis_generator[:show_num],
                         grid_shape=utils.squarest_grid_size(show_num),
                         image_shape=(128, 128)))

    d_loss_real = _get_d_real_loss(
        discriminator_on_data_logits)
    d_loss_fake = _get_d_fake_loss(discriminator_on_generator_logits)
    g_loss_gan = _get_g_loss(discriminator_on_generator_logits)

    d_loss = d_loss_real + d_loss_fake
    g_loss = g_loss_gan

    logit_discriminator_on_data = tf.reduce_mean(discriminator_on_data_logits)
    logit_discriminator_on_generator = tf.reduce_mean(
        discriminator_on_generator_logits)

    tf.summary.scalar('d_loss', d_loss)
    tf.summary.scalar('d_loss_real', d_loss_real)
    tf.summary.scalar('d_loss_fake', d_loss_fake)
    tf.summary.scalar('g_loss', g_loss)
    tf.summary.scalar('logit_real', logit_discriminator_on_data)
    tf.summary.scalar('logit_fake', logit_discriminator_on_generator)

    if gpu_idx == 0:
      self.d_loss_reals = []
      self.d_loss_fakes = []
      self.d_losses = []
      self.g_losses = []
    self.d_loss_reals.append(d_loss_real)
    self.d_loss_fakes.append(d_loss_fake)
    self.d_losses.append(d_loss)
    self.g_losses.append(g_loss)

    if gpu_idx == 0:
      self.get_vars()
      for var in self.sigma_ratio_vars:
        tf.summary.scalar(var.name, var)

  def get_vars(self):
    t_vars = tf.trainable_variables()
    self.d_vars = [var for var in t_vars if var.name.startswith('model/d_')]
    self.g_vars = [var for var in t_vars if var.name.startswith('model/g_')]
    self.sigma_ratio_vars = [var for var in t_vars if 'sigma_ratio' in var.name]
    self.all_vars = t_vars
