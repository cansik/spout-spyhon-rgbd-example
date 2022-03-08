import argparse
import logging
import threading
from datetime import datetime
from typing import Callable, Optional, List

import cv2
import numpy as np
import pyrealsense2 as rs
import visiongraph as vg
from simbi.model.DataField import DataField
from simbi.ui.annotations.BooleanAnnotation import BooleanAnnotation
from simbi.ui.annotations.EnumAnnotation import EnumAnnotation
from simbi.ui.annotations.TextAnnotation import TextAnnotation

from spacestream.codec.DepthCodec import DepthCodec
from spacestream.codec.DepthCodecType import DepthCodecType
from spacestream.codec.UniformHueColorization import UniformHueColorization
from spacestream.fbs import FrameBufferSharingServer


def linear_interpolate(x):
    return x


def ease_out_quad(x):
    # decode = sqrt(x)
    return x * (2 - x)


class SpaceStreamPipeline(vg.BaseGraph):

    def __init__(self, stream_name: str, input: vg.BaseInput, fbs_client: FrameBufferSharingServer,
                 codec: DepthCodecType = DepthCodecType.Linear,
                 min_distance: float = 0, max_distance: float = 6,
                 record: bool = False, masking: bool = False,
                 segnet: Optional[vg.InstanceSegmentationEstimator] = None, use_midas: bool = False,
                 multi_threaded: bool = False, handle_signals: bool = True):
        super().__init__(multi_threaded, False, handle_signals)

        self.stream_name = stream_name
        self.input = input
        self.fbs_client = fbs_client
        self.fps_tracer = vg.FPSTracer()

        self.depth_units: float = 0.001

        # options
        self.pipeline_fps = DataField("-") | TextAnnotation("Pipeline FPS", readonly=True)
        self.disable_preview = DataField(False) | BooleanAnnotation("Disable Preview")

        self.depth_codec: DepthCodec = codec.value()
        self.codec = DataField(codec) | EnumAnnotation("Codec")
        self.min_distance = min_distance
        self.max_distance = max_distance

        def codec_changed(c):
            self.depth_codec = c.value()
        self.codec.on_changed += codec_changed

        self.record = record
        self.recorder: Optional[vg.CV2VideoRecorder] = None

        self.show_preview = True

        self.masking = masking
        self.segmentation_network: Optional[vg.InstanceSegmentationEstimator] = None

        if self.masking:
            self.segmentation_network = segnet
            if isinstance(self.segmentation_network, vg.MediaPipePoseEstimator):
                self.segmentation_network.enable_segmentation = True
            self.add_nodes(self.segmentation_network)

        self.use_midas = use_midas
        self.midas_net: Optional[vg.MidasDepthEstimator] = None

        if self.use_midas:
            self.midas_net = vg.MidasDepthEstimator.create(vg.MidasConfig.MidasSmall)
            self.midas_net.prediction_bit_depth = 16
            self.add_nodes(self.midas_net)

        # events
        self.on_frame_ready: Optional[Callable[[np.ndarray], None]] = None

        self.add_nodes(self.input)

    def _init(self):
        super()._init()

        if threading.current_thread() is threading.main_thread():
            self.fbs_client.setup()

        # set colorizer min and max settings
        if isinstance(self.input, vg.RealSenseInput):
            if not self.use_midas:
                self.input.colorizer.set_option(rs.option.histogram_equalization_enabled, 0)
                self.input.colorizer.set_option(rs.option.min_distance, self.min_distance)
                self.input.colorizer.set_option(rs.option.max_distance, self.max_distance)

            # display intrinsics
            profiles = self.input.pipeline.get_active_profile()

            stream = profiles.get_stream(rs.stream.depth).as_video_stream_profile()
            intrinsics = stream.get_intrinsics()
            logging.info(f"Depth Intrinsics: {intrinsics}")

            stream = profiles.get_stream(rs.stream.color).as_video_stream_profile()
            intrinsics = stream.get_intrinsics()
            logging.info(f"RGB Intrinsics: {intrinsics}")

        if isinstance(self.input, vg.AzureKinectInput):
            from pyk4a import CalibrationType

            calibration = self.input.device.calibration
            mat = calibration.get_camera_matrix(CalibrationType.DEPTH)

            logging.info(f"Serial: {self.input.device.serial}")
            logging.info(f"Depth Intrinsics:\n{mat}")

    def get_intrinsics(self) -> np.ndarray:
        if isinstance(self.input, vg.RealSenseInput):
            profiles = self.input.pipeline.get_active_profile()

            stream = profiles.get_stream(rs.stream.color).as_video_stream_profile()
            intrinsics = stream.get_intrinsics()
            return np.array([[intrinsics.fx, 0, intrinsics.ppx],
                             [0, intrinsics.fy, intrinsics.ppy],
                             [0, 0, 1]])

        if isinstance(self.input, vg.AzureKinectInput):
            from pyk4a import CalibrationType

            calibration = self.input.device.calibration
            mat = calibration.get_camera_matrix(CalibrationType.DEPTH)
            return mat

        return np.ndarray(shape=(3, 3))

    def _process(self):
        ts, frame = self.input.read()

        if frame is None:
            return

        # start recording
        if self.record and self.recorder is None:
            h, w = frame.shape[:2]
            rw = w * 2
            rh = h
            time_str = datetime.now().strftime("%y-%m-%d-%H-%M-%S")
            output_file_path = f"recordings/{self.stream_name}-{time_str}.mp4"
            self.recorder = vg.CV2VideoRecorder(rw, rh, output_file_path, fps=self.input.fps)
            self.recorder.open()

        if self.masking:
            segmentations: List[vg.InstanceSegmentationResult] = self.segmentation_network.process(frame)
            for segment in segmentations:
                frame = self.mask_image(frame, segment.mask)

        if isinstance(self.input, vg.BaseDepthInput):
            if isinstance(self.input, vg.RealSenseInput):
                self.depth_units = self.input.depth_frame.get_units()

            if self.midas_net is not None:
                depth_buffer = self.midas_net.process(frame)
            else:
                depth_buffer = self.input

            depth = depth_buffer.depth_buffer

            if self.use_midas:
                depth = pow(2, 16) - depth

            # read depth map and create rgb-d image

            min_value = round(self.min_distance / self.depth_units)
            max_value = round(self.max_distance / self.depth_units)
            depth_map = self.depth_codec.encode(depth, min_value, max_value)

            # fix realsense image if it has been aligned to remove lines
            if isinstance(self.input, vg.RealSenseInput):
                depth_map = cv2.medianBlur(depth_map, 3)

            # resize to match rgb image if necessary
            if depth_map.shape != frame.shape:
                h, w = frame.shape[:2]
                depth_map = cv2.resize(depth_map, (w, h), interpolation=cv2.INTER_AREA)

            if self.masking:
                for segment in segmentations:
                    depth_map = self.mask_image(depth_map, segment.mask)

            rgbd = np.hstack((depth_map, frame))
        else:
            # just send rgb image for testing
            rgbd = frame

        if threading.current_thread() is threading.main_thread():
            # send rgb-d over spout
            bgrd = cv2.cvtColor(rgbd, cv2.COLOR_RGB2BGR)
            self.fbs_client.send(bgrd)

        if self.record and self.recorder is not None:
            self.recorder.add_image(bgrd)

        if not self.disable_preview.value and self.on_frame_ready is not None:
            self.on_frame_ready(rgbd)

        self.fps_tracer.update()
        self.pipeline_fps.value = f"{self.fps_tracer.smooth_fps:.2f}"

    def _release(self):
        if threading.current_thread() is threading.main_thread():
            self.fbs_client.release()

        super()._release()
        if self.record and self.recorder is not None:
            self.recorder.close()

    @staticmethod
    def mask_image(image: np.ndarray, mask: np.ndarray) -> np.ndarray:
        masked = cv2.bitwise_and(image, image, mask=mask)
        return masked

    @staticmethod
    def add_params(parser: argparse.ArgumentParser):
        pass

    def configure(self, args: argparse.Namespace):
        super().configure(args)
