import tensorflow as tf
import numpy as np
import time
import hparams as hp
from model import transformer
from preprocess import get_vocab
from pathlib import Path
from absl import app, flags

# Training hparams
hp.add("shuffle_buffer", 100, help="Shuffle buffer")
hp.add("batch_size", 1, help="Batch size")
hp.add("learning_rate", 0.01, help="Learning rate")


def get_dataset(dataset_path: Path, batch_size: int, shuffle_buffer: int, skip: int = 0):
    feature_description = {
        'text': tf.io.VarLenFeature(tf.int64)
    }

    def _parse_function(example_proto):
        example = tf.io.parse_single_example(example_proto, feature_description)
        return tf.sparse.to_dense(example["text"])

    ds = tf.data.TFRecordDataset(str(dataset_path))
    ds = ds.map(_parse_function)
    ds = ds.padded_batch(batch_size, padded_shapes=(-1,))
    ds = ds.shuffle(buffer_size=shuffle_buffer, seed=42)
    ds = ds.repeat()
    ds = ds.skip(skip)

    return ds


def train_loop(ds, transformer_decoder, global_step, ckpt_manager, optimizer, train_summary_writer,
               checkpoint_every, summarize_every, continuous=True):
    loss_object = tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True, reduction='none')
    train_step_signature = [tf.TensorSpec(shape=(None, None), dtype=tf.int64)]

    def calculate_loss(real, pred):
        # Masks padded tokens from loss_object
        mask = tf.math.logical_not(tf.math.equal(real, 0))
        loss_ = loss_object(real, pred)

        return tf.reduce_mean(tf.boolean_mask(loss_, mask))

    @tf.function(input_signature=train_step_signature, experimental_relax_shapes=True)
    def train_step(batch):
        tar_inp = batch[:, :-1]
        tar_real = batch[:, 1:]

        mask = transformer.create_masks(tar_inp)

        with train_summary_writer.as_default():
            with tf.GradientTape() as tape:
                with tf.summary.record_if(tf.math.equal(tf.math.mod(global_step, summarize_every), 0)):
                    predictions, _ = transformer_decoder(tar_inp, True, mask)
                loss = calculate_loss(tar_real, predictions)

            gradients = tape.gradient(loss, transformer_decoder.trainable_variables)
            optimizer.apply_gradients(zip(gradients, transformer_decoder.trainable_variables))

            tf.summary.scalar("loss", loss)
            tf.summary.scalar("gradient_norm", tf.linalg.global_norm(gradients))

        return loss

    steps_start = time.time()

    for batch in ds:
        global_step.assign_add(1)
        tf.summary.experimental.set_step(global_step)

        # Take a gradient step
        loss = train_step(batch)

        if global_step.numpy() == 1:
            print("Number of trainable parameters: {}".format(
                np.sum([np.prod(v.get_shape().as_list()) for v in transformer_decoder.trainable_variables])))

        # Print intermediate metrics
        if global_step.numpy() % 100 == 0:
            print('Step: {} Loss: {:.4f} ({:.3f}s)'.format(
                global_step.numpy(), loss, time.time() - steps_start))
            steps_start = time.time()

        # Checkpoint every X step
        if global_step.numpy() % checkpoint_every == 0:
            ckpt_save_path = ckpt_manager.save(checkpoint_number=global_step)
            print("Saving checkpoint at '{}'".format(ckpt_save_path))

            if not continuous:
                break


def main(argv):
    vocab_size = get_vocab(Path(flags.FLAGS.vocab)).vocab_size

    # Model
    transformer_decoder = transformer.TransformerOnlyDecoder(vocab_size)

    # Optimizer
    optimizer = tf.keras.optimizers.SGD(hp.get("learning_rate"))

    # Global step counter
    global_step = tf.Variable(0, name="global_step", trainable=False, dtype=tf.int64)

    # Checkpointing
    checkpoint_path = Path(flags.FLAGS.checkpoint_path)
    ckpt = tf.train.Checkpoint(transformer_decoder=transformer_decoder, optimizer=optimizer,
                               global_step=global_step)
    ckpt_manager = tf.train.CheckpointManager(ckpt, str(checkpoint_path), max_to_keep=5)
    if ckpt_manager.latest_checkpoint:
        ckpt.restore(ckpt_manager.latest_checkpoint)
        print("Restored checkpoint from: {}".format(ckpt_manager.latest_checkpoint))

    # Tensorboard events
    train_log_dir = str(checkpoint_path / "events")
    train_summary_writer = tf.summary.create_file_writer(train_log_dir)

    # Training dataset
    ds = get_dataset(Path(flags.FLAGS.data), hp.get("batch_size"), hp.get("shuffle_buffer"),
                     skip=global_step.numpy())

    try:
        train_loop(ds, transformer_decoder, global_step, ckpt_manager, optimizer, train_summary_writer,
                   flags.FLAGS.checkpoint_every, flags.FLAGS.summarize_every, flags.FLAGS.continuous)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    flags.DEFINE_string("data", None, help="Training data tfrecord file")
    flags.DEFINE_string("vocab", None, help="Vocab file")
    flags.DEFINE_string("checkpoint_path", None, help="Checkpoint path")
    flags.DEFINE_integer("checkpoint_every", 1000, help="Checkpoint every X step")
    flags.DEFINE_boolean("continuous", True, help="Whether to continue training after checkpointing")
    flags.DEFINE_integer("summarize_every", 1, help="Summarize model stats every X step")
    flags.mark_flags_as_required(["data", "vocab", "checkpoint_path"])

    app.run(main)
