"""
Katna 抽帧工作进程

Katna 内部使用 multiprocessing.Pool，在 Windows 上必须作为主模块运行。
本脚本通过 subprocess 被调用，避免污染主进程的 import 流程。

用法：
    python -m bilibot.video_understanding.katna_worker <video_path> <output_dir> <no_of_frames>
"""
import os
import sys

from Katna.video import Video
from Katna.writer import KeyFrameDiskWriter


def main():
    if len(sys.argv) < 4:
        print("Usage: katna_worker.py <video_path> <output_dir> <no_of_frames>")
        sys.exit(1)

    video_path = sys.argv[1]
    output_dir = sys.argv[2]
    no_of_frames = int(sys.argv[3])

    os.makedirs(output_dir, exist_ok=True)

    vd = Video()
    diskwriter = KeyFrameDiskWriter(location=output_dir)
    vd.extract_video_keyframes(
        no_of_frames=no_of_frames,
        file_path=video_path,
        writer=diskwriter,
    )

    files = sorted(os.listdir(output_dir))
    for name in files:
        print(name)


if __name__ == "__main__":
    main()
