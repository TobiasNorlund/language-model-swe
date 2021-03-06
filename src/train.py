import tensorflow as tf
import numpy as np
import time
import json
import hparams as hp
from model import transformer
from optimizer import get_optimizer
from preprocess import get_vocab
from pathlib import Path
from absl import app, flags

# Training hparams
hp.add("shuffle_buffer", 1, help="Shuffle buffer")
hp.add("max_tokens", 100, help="Max tokens")
hp.add("max_seq_len", 600, help="Max sequence len")


def get_dataset(dataset_path: Path, max_tokens: int, max_seq_len: int, shuffle_buffer: int, skip: int = 0):
    def parse_json(json_string_tensor):
        encoded = json.loads(json_string_tensor.numpy())["encoded"]
        return tf.constant(encoded, dtype=tf.int64, shape=[len(encoded)])

    def parse_json_fn(text):
        return tf.py_function(parse_json, inp=[text], Tout=tf.int64)

    boundaries = [i for i in range(1, max_seq_len + 1) if max_tokens % i == 0]
    batch_sizes = [int(max_tokens / i) for i in range(1, max_seq_len + 1) if max_tokens % i == 0] + [1]

    ds = tf.data.TextLineDataset(str(dataset_path))
    ds = ds.map(parse_json_fn)
    ds = ds.apply(tf.data.experimental.bucket_by_sequence_length(lambda x: tf.shape(x),
                                                                 boundaries,
                                                                 batch_sizes, padded_shapes=[None]))
    ds = ds.shuffle(buffer_size=shuffle_buffer, seed=42)
    ds = ds.repeat()
    ds = ds.skip(skip)
    ds = ds.prefetch(100)

    return ds


def train_loop(ds, transformer_decoder, global_step, num_examples_processed, ckpt_manager, optimizer, learning_rate,
               train_summary_writer, checkpoint_every, summarize_every, continuous=True):
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
            with tf.summary.record_if(tf.math.equal(tf.math.mod(global_step, summarize_every), 0)):
                with tf.GradientTape() as tape:
                    predictions, _ = transformer_decoder(tar_inp, True, mask)
                    loss = calculate_loss(tar_real, predictions)

                vars = transformer_decoder.trainable_variables
                gradients = tape.gradient(loss, vars)
                optimizer.apply_gradients(zip(gradients, transformer_decoder.trainable_variables))

                for i in range(len(vars)):
                    tf.summary.scalar("gradient/" + vars[i].name, tf.linalg.norm(gradients[i]))

            tf.summary.scalar("loss", loss)
            tf.summary.scalar("gradient_norm", tf.linalg.global_norm(gradients))
            tf.summary.scalar("learning_rate",
                              learning_rate if type(learning_rate) is float else learning_rate(global_step))

        return loss

    steps_start = time.time()

    for batch in ds:
        global_step.assign_add(1)
        num_examples_processed.assign_add(tf.cast(tf.shape(batch)[0], num_examples_processed.dtype))
        tf.summary.experimental.set_step(global_step)

        # Take a gradient step
        loss = train_step(batch)

        if global_step.numpy() == 1:
            print("Number of trainable parameters: {}".format(
                np.sum([np.prod(v.get_shape().as_list()) for v in transformer_decoder.trainable_variables])))

        # Print intermediate metrics
        if global_step.numpy() % 100 == 0:
            print('Step: {}\tLoss: {:.4f}\tNum examples: {}\tTime: {:.3f}s'.format(
                global_step.numpy(), loss, num_examples_processed.numpy(), time.time() - steps_start))
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
    optimizer, learning_rate = get_optimizer()

    # Counters
    global_step = tf.Variable(0, name="global_step", trainable=False, dtype=tf.int64)
    num_examples_processed = tf.Variable(0, name="num_examples_processed", trainable=False, dtype=tf.int64)

    # Checkpointing
    checkpoint_path = Path(flags.FLAGS.checkpoint_path)
    ckpt = tf.train.Checkpoint(transformer_decoder=transformer_decoder, optimizer=optimizer,
                               global_step=global_step, num_examples_processed=num_examples_processed)
    ckpt_manager = tf.train.CheckpointManager(ckpt, str(checkpoint_path), max_to_keep=5)
    if ckpt_manager.latest_checkpoint:
        ckpt.restore(ckpt_manager.latest_checkpoint)
        print("Restored checkpoint from: {}".format(ckpt_manager.latest_checkpoint))

    # Tensorboard events
    train_log_dir = str(checkpoint_path / "events")
    train_summary_writer = tf.summary.create_file_writer(train_log_dir)

    # Training dataset
    ds = get_dataset(Path(flags.FLAGS.data), hp.get("max_tokens"), hp.get("max_seq_len"), hp.get("shuffle_buffer"),
                     skip=global_step.numpy())

    try:
        train_loop(ds, transformer_decoder, global_step, num_examples_processed, ckpt_manager, optimizer,
                   learning_rate, train_summary_writer, flags.FLAGS.checkpoint_every, flags.FLAGS.summarize_every,
                   flags.FLAGS.continuous)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    flags.DEFINE_string("data", None, help="Training data tfrecord file")
    flags.DEFINE_string("vocab", None, help="Vocab file")
    flags.DEFINE_string("checkpoint_path", None, help="Checkpoint path")
    flags.DEFINE_integer("checkpoint_every", 1000, help="Checkpoint every X step")
    flags.DEFINE_boolean("continuous", True, help="Whether to continue training after checkpointing")
    flags.DEFINE_integer("summarize_every", 50, help="Summarize model stats every X step")
    flags.mark_flags_as_required(["data", "vocab", "checkpoint_path"])

    app.run(main)
