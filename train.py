#!/usr/bin/env python

# Modified Horovod MNIST example

import os
import sys
import time

import horovod.tensorflow as hvd
import numpy as np
import tensorflow as tf
import graphics
from utils import ResultLogger

learn = tf.contrib.learn

# Surpress verbose warnings
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'


DEBUG = True


def _print(*args, **kwargs):
    if hvd.rank() == 0:
        print(*args, **kwargs)


def init_visualizations(hps, logdir, model):

    def sample_batch(y, eps, model):
        n_batch = hps.local_batch_train
        xs_A = []
        xs_B = []
        for i in range(int(np.ceil(len(eps) / n_batch))):
            xs_A.append(model.sample_A(
                y[i*n_batch:i*n_batch + n_batch], eps[i*n_batch:i*n_batch + n_batch]))
            xs_B.append(model.sample_B(
                y[i*n_batch:i*n_batch + n_batch], eps[i*n_batch:i*n_batch + n_batch]))
        return np.concatenate(xs_A), np.concatenate(xs_B)

    def draw_samples(epoch):
        if hvd.rank() != 0:
            return

        rows = 10 if hps.image_size <= 64 else 4
        cols = rows
        n_batch = rows*cols
        y = np.asarray([_y % hps.n_y for _y in (
            list(range(cols)) * rows)], dtype='int32')

        # temperatures = [0., .25, .5, .626, .75, .875, 1.] #previously
        temperatures = [0., .25, .5, .6, .7, .8, .9, 1.]

        x_samples = {'A': [], 'B': []}
        for model_name in ['A', 'B']:
            for t in temperatures:
                xs_A, xs_B = sample_batch(y, [t]*n_batch, model)
                x_samples['A'].append(xs_A)
                x_samples['B'].append(xs_B)

            # previously: 0, .25, .5, .625, .75, .875, 1.
            for i in range(len(x_samples[model_name])):
                x_sample = np.reshape(
                    x_samples[model_name][i], (n_batch, hps.image_size, hps.image_size, 3))
                graphics.save_raster(x_sample, logdir +
                                     '{}_epoch_{}_sample_{}.png'.format(model_name, epoch, i))

    return draw_samples

# ===
# Code for getting data
# ===


def get_data(hps, sess):
    if hps.image_size == -1:
        hps.image_size = {'edges2shoes': 32, 'mnist': 32, 'cifar10': 32, 'imagenet-oord': 64,
                          'imagenet': 256, 'celeba': 256, 'lsun_realnvp': 64, 'lsun': 256}[hps.problem]
    if hps.n_test == -1:
        hps.n_test = {'edges2shoes': 200, 'mnist': 10000, 'cifar10': 10000,
                      'imagenet-oord': 50000, 'imagenet': 50000,
                      'celeba': 3000, 'lsun_realnvp': 300*hvd.size(), 'lsun': 300*hvd.size()}[hps.problem]
    hps.n_y = {'edges2shoes': 10, 'mnist': 10, 'cifar10': 10, 'imagenet-oord': 1000,
               'imagenet': 1000, 'celeba': 1, 'lsun_realnvp': 1, 'lsun': 1}[hps.problem]
    if hps.data_dir == "":
        hps.data_dir = {'edges2shoes': 'edges2shoes', 'mnist': None, 'cifar10': None,
                        'imagenet-oord': '/mnt/host/imagenet-oord-tfr',
                        'imagenet': '/mnt/host/imagenet-tfr',
                        'celeba': '/mnt/host/celeba-reshard-tfr',
                        'lsun_realnvp': '/mnt/host/lsun_realnvp', 'lsun': '/mnt/host/lsun'}[hps.problem]

    if hps.problem == 'lsun_realnvp':
        hps.rnd_crop = True
    else:
        hps.rnd_crop = False

    if hps.category:
        hps.data_dir += ('/%s' % hps.category)

    # Use anchor_size to rescale batch size based on image_size
    s = hps.anchor_size
    hps.local_batch_train = hps.n_batch_train * \
        s * s // (hps.image_size * hps.image_size)
    hps.local_batch_test = {64: 50, 32: 25, 16: 10, 8: 5, 4: 2, 2: 2, 1: 1}[
        hps.local_batch_train]  # round down to closest divisor of 50
    hps.local_batch_init = hps.n_batch_init * \
        s * s // (hps.image_size * hps.image_size)

    print("Rank {} Batch sizes Train {} Test {} Init {}".format(
        hvd.rank(), hps.local_batch_train, hps.local_batch_test, hps.local_batch_init))

    if hps.problem in ['imagenet-oord', 'imagenet', 'celeba', 'lsun_realnvp', 'lsun']:
        hps.direct_iterator = True
        import data_loaders.get_data as v
        train_iterator, test_iterator, data_init = \
            v.get_data(sess, hps.data_dir, hvd.size(), hvd.rank(), hps.pmap, hps.fmap, hps.local_batch_train,
                       hps.local_batch_test, hps.local_batch_init, hps.image_size, hps.rnd_crop)

    elif hps.problem in ['mnist', 'cifar10']:
        hps.direct_iterator = False
        import data_loaders.get_mnist_cifar_joint as v
        train_iterator_A, test_iterator_A, data_init_A, train_iterator_B, test_iterator_B, data_init_B = \
            v.get_data(hps.problem, hvd.size(), hvd.rank(), hps.dal, hps.local_batch_train,
                       hps.local_batch_test, hps.local_batch_init, hps.image_size)
    elif hps.problem in ['edges2shoes']:
        hps.direct_iterator = False
        import data_loaders.get_edges_shoes_joint as v
        train_iterator_A, test_iterator_A, data_init_A, train_iterator_B, test_iterator_B, data_init_B = \
            v.get_data(hps.problem, hvd.size(), hvd.rank(), hps.dal, hps.local_batch_train,
                       hps.local_batch_test, hps.local_batch_init, hps.image_size)
    else:
        raise Exception()

    return train_iterator_A, test_iterator_A, data_init_A, train_iterator_B, test_iterator_B, data_init_B


def process_results(results):
    stats = ['loss', 'bits_x', 'bits_y', 'pred_loss', 'code_loss']
    assert len(stats) == results.shape[0]
    res_dict = {}
    for i in range(len(stats)):
        res_dict[stats[i]] = "{:.4f}".format(results[i])
    return res_dict


def main(hps):

    # Initialize Horovod.
    hvd.init()

    # Create tensorflow session
    sess = tensorflow_session()

    # Download and load dataset.
    tf.set_random_seed(hvd.rank() + hvd.size() * hps.seed)
    np.random.seed(hvd.rank() + hvd.size() * hps.seed)

    # Get data and set train_its and valid_its
    train_iterator_A, test_iterator_A, data_init_A, train_iterator_B, test_iterator_B, data_init_B = get_data(
        hps, sess)
    hps.train_its, hps.test_its, hps.full_test_its = get_its(hps)

    # Create log dir
    logdir = os.path.abspath(hps.logdir) + "/"
    if not os.path.exists(logdir):
        os.mkdir(logdir)

    # Set up restore path
    if hps.inference:
        if hps.restore_path_A == '':
            hps.restore_path_A = os.path.join(
                hps.logdir, 'model_A_best_loss.ckpt')
        if hps.restore_path_B == '':
            hps.restore_path_B = os.path.join(
                hps.logdir, 'model_B_best_loss.ckpt')

    # Create model
    import model
    train_iterators = {'A': train_iterator_A, 'B': train_iterator_B}
    test_iterators = {'A': test_iterator_A, 'B': test_iterator_B}
    data_inits = {'A': data_init_A, 'B': data_init_B}
    model = model.model(sess, hps,
                        train_iterators, test_iterators, data_inits)
    # Initialize visualization functions
    visualise = init_visualizations(hps, logdir, model)
    if not hps.inference:
        train(sess, model, hps, logdir, visualise)
    else:
        iterators = {'A': test_iterator_A, 'B': test_iterator_B}
        infer(sess, model, hps, iterators, hps.full_test_its)


def train(sess, model, hps, logdir, visualise):
    _print(hps)
    _print('Starting training. Logging to', logdir)
    _print('epoch n_processed n_images ips dtrain dtest dsample dtot train_results test_results msg')

    # Train
    sess.graph.finalize()
    n_processed = 0
    n_images = 0
    train_time = 0.0
    test_loss_best = {'A': 999999, 'B': 999999}

    if hvd.rank() == 0:
        train_logger = {'A': ResultLogger(logdir + "train_A.txt", **hps.__dict__),
                        'B': ResultLogger(logdir + "train_B.txt", **hps.__dict__)}
        test_logger = {'A': ResultLogger(logdir + "test_A.txt", **hps.__dict__),
                       'B': ResultLogger(logdir + "test_B.txt", **hps.__dict__)}

    tcurr = time.time()
    for epoch in range(1, hps.epochs):
        t = time.time()
        train_results = {'A': [], 'B': []}
        for it in range(hps.train_its):

            # Set learning rate, linearly annealed from 0 in the first hps.epochs_warmup epochs.
            lr = hps.lr * min(1., n_processed /
                              (hps.n_train * hps.epochs_warmup))

            # Run a training step synchronously.
            _t = time.time()
            x_A, y_A, x_B, y_B = model.get_train_data()
            train_results_A, train_results_B = model.train(
                lr, x_A, y_A, x_B, y_B)
            train_results['A'] += [train_results_A]
            train_results['B'] += [train_results_B]
            if hps.verbose and hvd.rank() == 0:
                _print(n_processed, time.time()-_t, train_results['A'][-1])
                _print(n_processed, time.time()-_t, train_results['B'][-1])
                sys.stdout.flush()

            # Images seen wrt anchor resolution
            n_processed += hvd.size() * hps.n_batch_train
            # Actual images seen at current resolution
            n_images += hvd.size() * hps.local_batch_train

        train_results['A'] = np.mean(np.asarray(train_results['A']), axis=0)
        train_results['B'] = np.mean(np.asarray(train_results['B']), axis=0)

        dtrain = time.time() - t
        ips = (hps.train_its * hvd.size() * hps.local_batch_train) / dtrain
        train_time += dtrain

        if hvd.rank() == 0:
            train_logger['A'].log(epoch=epoch, n_processed=n_processed, n_images=n_images, train_time=int(
                train_time), **process_results(train_results['A']))
            train_logger['B'].log(epoch=epoch, n_processed=n_processed, n_images=n_images, train_time=int(
                train_time), **process_results(train_results['B']))

        if epoch < 10 or (epoch < 50 and epoch % 10 == 0) or epoch % hps.epochs_full_valid == 0:
            test_results = {'A': [], 'B': []}
            msg = {'A': 'A', 'B': 'B'}

            t = time.time()
            # model.polyak_swap()

            if epoch % hps.epochs_full_valid == 0:
                # Full validation run
                for it in range(hps.full_test_its):
                    x_A, y_A, x_B, y_B = model.get_test_data()
                    test_results['A'] += [model.test_A(x_A, y_A, x_B, y_B)]
                    test_results['B'] += [model.test_B(x_A, y_A, x_B, y_B)]
                test_results['A'] = np.mean(
                    np.asarray(test_results['A']), axis=0)
                test_results['B'] = np.mean(
                    np.asarray(test_results['B']), axis=0)

                if hvd.rank() == 0:
                    test_logger['A'].log(epoch=epoch, n_processed=n_processed,
                                         n_images=n_images, **process_results(test_results['A']))
                    test_logger['B'].log(epoch=epoch, n_processed=n_processed,
                                         n_images=n_images, **process_results(test_results['B']))
                    # Save checkpoint
                    if test_results['A'][0] < test_loss_best['A']:
                        test_loss_best['A'] = test_results['A'][0]
                        model.save_A(logdir+"model_A_best_loss.ckpt")
                        msg['A'] += ' *'
                    if test_results['B'][0] < test_loss_best['B']:
                        test_loss_best['B'] = test_results['B'][0]
                        model.save_B(logdir+"model_B_best_loss.ckpt")
                        msg['B'] += ' *'

            dtest = time.time() - t

            # Sample
            t = time.time()
            if epoch == 1 or epoch == 10 or epoch % hps.epochs_full_sample == 0:
                visualise(epoch)
            dsample = time.time() - t

            if hvd.rank() == 0:
                dcurr = time.time() - tcurr
                tcurr = time.time()
                msg['A'] += ', train_time: {}'.format(int(train_time))
                msg['B'] += ', train_time: {}'.format(int(train_time))
                _print(epoch, n_processed, n_images, "{:.1f} {:.1f} {:.1f} {:.1f} {:.1f}".format(
                    ips, dtrain, dtest, dsample, dcurr), train_results['A'], test_results['A'], msg['A'])
                _print(epoch, n_processed, n_images, "{:.1f} {:.1f} {:.1f} {:.1f} {:.1f}".format(
                    ips, dtrain, dtest, dsample, dcurr), train_results['B'], test_results['B'], msg['B'])
            # model.polyak_swap()

    if hvd.rank() == 0:
        _print("Finished!")


def infer(sess, model, hps, iterators, its):
    from tqdm import tqdm
    assert hps.restore_path_A != ''
    assert hps.restore_path_B != ''

    xs_A, xs_B = [], []
    zs_A, zs_B = [], []
    for it in tqdm(range(its)):
        x_A, y_A = iterators['A']()
        x_B, y_B = iterators['B']()

        # A2B
        z_A = model.encode(x_A, y_A, 'model_A')
        x_B_recon = model.decode(y_B, z_A, 'model_B')
        xs_B.append(x_B_recon)
        zs_A.append(z_A)

        # B2A
        z_B = model.encode(x_B, y_B, 'model_B')
        x_A_recon = model.decode(y_A, z_B, 'model_A')
        xs_A.append(x_A_recon)
        zs_B.append(z_B)

    x_A = np.concatenate(xs_A, axis=0)
    z_A = np.concatenate(zs_A, axis=0)
    x_B = np.concatenate(xs_B, axis=0)
    z_B = np.concatenate(zs_B, axis=0)

    np.save(os.path.join(hps.logdir, 'z_A'), z_A)
    np.save(os.path.join(hps.logdir, 'z_B'), z_B)

    from utils import npy2img
    npy2img(os.path.join(hps.logdir, 'B2A'), x_A)
    npy2img(os.path.join(hps.logdir, 'A2B'), x_B)

    return x_A, z_A, x_B, z_B

# Get number of training and validation iterations


def get_its(hps):
    # These run for a fixed amount of time. As anchored batch is smaller, we've actually seen fewer examples
    train_its = int(np.ceil(hps.n_train / (hps.n_batch_train * hvd.size())))
    test_its = int(np.ceil(hps.n_test / (hps.n_batch_train * hvd.size())))
    train_epoch = train_its * hps.n_batch_train * hvd.size()

    # Do a full validation run
    if hvd.rank() == 0:
        print(hps.n_test, hps.local_batch_test, hvd.size())
    assert hps.n_test % (hps.local_batch_test * hvd.size()) == 0
    full_test_its = hps.n_test // (hps.local_batch_test * hvd.size())

    if hvd.rank() == 0:
        print("Train epoch size: " + str(train_epoch))
    return train_its, test_its, full_test_its


'''
Create tensorflow session with horovod
'''


def tensorflow_session():
    # Init session and params
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    # Pin GPU to local rank (one GPU per process)
    config.gpu_options.visible_device_list = str(hvd.local_rank())
    sess = tf.Session(config=config)
    return sess


if __name__ == "__main__":

    # This enables a ctr-C without triggering errors
    import signal
    signal.signal(signal.SIGINT, lambda x, y: sys.exit(0))

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action='store_true', help="Verbose mode")
    parser.add_argument("--restore_path_A", type=str, default='',
                        help="Location of checkpoint to restore model A")
    parser.add_argument("--restore_path_B", type=str, default='',
                        help="Location of checkpoint to restore model B")
    parser.add_argument("--inference", action="store_true",
                        help="Use in inference mode")
    parser.add_argument("--logdir", type=str,
                        default='./logs', help="Location to save logs")

    # Dataset hyperparams:
    parser.add_argument("--problem", type=str, default='cifar10',
                        help="Problem (mnist/cifar10/imagenet")
    parser.add_argument("--category", type=str,
                        default='', help="LSUN category")
    parser.add_argument("--data_dir", type=str, default='',
                        help="Location of data")
    parser.add_argument("--dal", type=int, default=1,
                        help="Data augmentation level: 0=None, 1=Standard, 2=Extra")

    # New dataloader params
    parser.add_argument("--fmap", type=int, default=1,
                        help="# Threads for parallel file reading")
    parser.add_argument("--pmap", type=int, default=16,
                        help="# Threads for parallel map")

    # Optimization hyperparams:
    parser.add_argument("--n_train", type=int,
                        default=50000, help="Train epoch size")
    parser.add_argument("--n_test", type=int, default=-
                        1, help="Valid epoch size")
    parser.add_argument("--n_batch_train", type=int,
                        default=64, help="Minibatch size")
    parser.add_argument("--n_batch_test", type=int,
                        default=50, help="Minibatch size")
    parser.add_argument("--n_batch_init", type=int, default=256,
                        help="Minibatch size for data-dependent init")
    parser.add_argument("--optimizer", type=str,
                        default="adamax", help="adam or adamax")
    parser.add_argument("--lr", type=float, default=0.001,
                        help="Base learning rate")
    parser.add_argument("--beta1", type=float, default=.9, help="Adam beta1")
    parser.add_argument("--polyak_epochs", type=float, default=1,
                        help="Nr of averaging epochs for Polyak and beta2")
    parser.add_argument("--weight_decay", type=float, default=1.,
                        help="Weight decay. Switched off by default.")
    parser.add_argument("--epochs", type=int, default=1000000,
                        help="Total number of training epochs")
    parser.add_argument("--epochs_warmup", type=int,
                        default=10, help="Warmup epochs")
    parser.add_argument("--epochs_full_valid", type=int,
                        default=5, help="Epochs between valid")
    parser.add_argument("--gradient_checkpointing", type=int,
                        default=1, help="Use memory saving gradients")

    # Model hyperparams:
    parser.add_argument("--image_size", type=int,
                        default=-1, help="Image size")
    parser.add_argument("--anchor_size", type=int, default=32,
                        help="Anchor size for deciding batch size")
    parser.add_argument("--width", type=int, default=512,
                        help="Width of hidden layers")
    parser.add_argument("--depth", type=int, default=32,
                        help="Depth of network")
    parser.add_argument("--weight_y", type=float, default=0.00,
                        help="Weight of log p(y|x) in weighted loss")
    parser.add_argument("--n_bits_x", type=int, default=8,
                        help="Number of bits of x")
    parser.add_argument("--n_levels", type=int, default=3,
                        help="Number of levels")

    # Synthesis/Sampling hyperparameters:
    parser.add_argument("--n_sample", type=int, default=1,
                        help="minibatch size for sample")
    parser.add_argument("--epochs_full_sample", type=int,
                        default=5, help="Epochs between full scale sample")

    # Ablation
    parser.add_argument("--learntop", action="store_true",
                        help="Learn spatial prior")
    parser.add_argument("--ycond", action="store_true",
                        help="Use y conditioning")
    parser.add_argument("--seed", type=int, default=0, help="Random seed")
    parser.add_argument("--flow_permutation", type=int, default=2,
                        help="Type of flow. 0=reverse (realnvp), 1=shuffle, 2=invconv (ours)")
    parser.add_argument("--flow_coupling", type=int, default=0,
                        help="Coupling type: 0=additive, 1=affine")

    # Pix2pix
    parser.add_argument("--joint_train", action="store_true",
                        help="Get each other's code to supervise latent space")
    parser.add_argument("--flip_color", action="store_true",
                        help="Whether flip the color of mnist")
    parser.add_argument("--code_loss_type", type=str, default='code_last',
                        help="code_all/code_last/B_all")
    parser.add_argument("--code_loss_fn", type=str, default='l2',
                        help="l2/l1")
    parser.add_argument("--code_loss_scale", type=float, default=1.0,
                        help="Scalar that is used to time the code_loss")
    parser.add_argument("--mle_loss_scale", type=float, default=1.0,
                        help="Scalar that is used to time the bits_x")
    hps = parser.parse_args()  # So error if typo
    main(hps)
