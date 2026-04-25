#!/usr/bin/env python3
"""Trim an mcap rosbag in-place so it starts at max(first_ts of required topics).

Used by app.py to eliminate the leading single-sensor window caused by DDS
discovery lag (LiDAR typically lands before camera after `ros2 bag record`
subscribes). Run under ROS2-sourced environment so rosbag2_py imports.

Usage:
    python3 trim_bag.py <bag_dir> <topic1> [topic2 ...]

Prints a single-line summary. Exit 0 on success (even if trim was a no-op),
non-zero on error.
"""
import os
import shutil
import sys

from rosbag2_py import (SequentialReader, SequentialWriter,
                        StorageOptions, ConverterOptions)


def main():
    if len(sys.argv) < 3:
        print('usage: trim_bag.py <bag_dir> <topic>...', file=sys.stderr)
        return 2

    bag_dir = sys.argv[1]
    required = set(sys.argv[2:])

    if not os.path.isdir(bag_dir):
        print(f'trim: bag dir missing: {bag_dir}', file=sys.stderr)
        return 3

    # Pass 1: find first timestamp per required topic + collect topic metadata
    reader = SequentialReader()
    reader.open(StorageOptions(uri=bag_dir, storage_id='mcap'),
                ConverterOptions('', ''))
    topic_metas = {tm.name: tm for tm in reader.get_all_topics_and_types()}
    first_ts = {}
    while reader.has_next():
        topic, _data, ts = reader.read_next()
        if topic in required and topic not in first_ts:
            first_ts[topic] = ts
            if len(first_ts) == len(required):
                break
    del reader

    missing = required - set(first_ts)
    if missing:
        print(f'trim: skip, missing topics: {sorted(missing)}')
        return 0

    sync_start = max(first_ts.values())
    lag_ms = (sync_start - min(first_ts.values())) / 1e6
    if lag_ms < 50:
        print(f'trim: skip, leading lag only {lag_ms:.1f}ms')
        return 0

    # Pass 2: rewrite to sibling tmp bag, keeping only messages >= sync_start
    parent = os.path.dirname(bag_dir)
    basename = os.path.basename(bag_dir)
    tmp_dir = os.path.join(parent, basename + '.trim_tmp')
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)

    reader = SequentialReader()
    reader.open(StorageOptions(uri=bag_dir, storage_id='mcap'),
                ConverterOptions('', ''))
    writer = SequentialWriter()
    writer.open(StorageOptions(uri=tmp_dir, storage_id='mcap'),
                ConverterOptions('', ''))
    for tm in topic_metas.values():
        writer.create_topic(tm)

    kept = 0
    dropped = 0
    while reader.has_next():
        topic, data, ts = reader.read_next()
        if ts < sync_start:
            dropped += 1
            continue
        writer.write(topic, data, ts)
        kept += 1
    del reader
    del writer

    try:
        shutil.rmtree(bag_dir)
        os.rename(tmp_dir, bag_dir)
    except OSError as e:
        print(f'trim: swap failed: {e}', file=sys.stderr)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return 4

    print(f'trim: lag={lag_ms:.1f}ms kept={kept} dropped={dropped}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
