'''
(*)~----------------------------------------------------------------------------------
 Pupil - eye tracking platform
 Copyright (C) 2012-2016  Pupil Labs

 Distributed under the terms of the GNU Lesser General Public License (LGPL v3.0).
 License details are in the file license.txt, distributed as part of this software.
----------------------------------------------------------------------------------~(*)
'''

import os,sys
import av
assert av.__version__ >= '0.2.5'

av.logging.set_level(av.logging.ERROR)

from . import Base_Source, InitialisationError

import numpy as np
from time import time,sleep
from fractions import Fraction
from  multiprocessing import cpu_count
#logging
import logging
logger = logging.getLogger(__name__)

class FileCaptureError(Exception):
    """General Exception for this module"""
    def __init__(self, arg):
        super(FileCaptureError, self).__init__()
        self.arg = arg

class EndofVideoFileError(Exception):
    """docstring for EndofVideoFileError"""
    def __init__(self, arg):
        super(EndofVideoFileError, self).__init__()
        self.arg = arg


class FileSeekError(Exception):
    """docstring for EndofVideoFileError"""
    def __init__(self):
        super(FileSeekError, self).__init__()


class Frame(object):
    """docstring of Frame"""
    def __init__(self, timestamp,av_frame,index):
        self._av_frame = av_frame
        self.timestamp = timestamp
        self.index = index
        self._img = None
        self._gray = None
        self.jpeg_buffer = None
        self.yuv_buffer = None
        self.height,self.width = av_frame.height,av_frame.width

    def copy(self):
        return Frame(self.timestamp,self._av_frame,self.index)

    @property
    def img(self):
        if self._img is None:
            self._img = self._av_frame.to_nd_array(format='bgr24')
        return self._img

    @property
    def bgr(self):
        return self.img

    @property
    def gray(self):
        if self._gray is None:
            self._gray = np.frombuffer(self._av_frame.planes[0],np.uint8).reshape(self.height,self.width)
        return self._gray



class File_Source(Base_Source):
    """
    simple file capture.
    """
    def __init__(self,g_pool,source_path=None,timestamps=None,*args,**kwargs):
        if not source_path or not os.path.isfile(source_path):
            raise InitialisationError()

        super(File_Source,self).__init__(g_pool)
        self.display_time = 0.
        self.target_frame_idx = 0

        self.slowdown = 0.0
        self.source_path = source_path
        self.container = av.open(source_path)

        try:
            self.video_stream = next(s for s in self.container.streams if s.type=="video")# looking for the first videostream
            logger.debug("loaded videostream: %s"%self.video_stream)
            self.video_stream.thread_count = cpu_count()
        except StopIteration:
            self.video_stream = None
            logger.error("No videostream found in media container")

        try:
            self.audio_stream = next(s for s in self.container.streams if s.type=='audio')# looking for the first audiostream
            logger.debug("loaded audiostream: %s"%self.audio_stream)
        except StopIteration:
            self.audio_stream = None
            logger.debug("No audiostream found in media container")

        if not self.video_stream and not self.audio_stream:
            raise InitialisationError()

        #we will use below for av playback
        # self.selected_streams = [s for s in (self.video_stream,self.audio_stream) if s]
        # self.av_packet_iterator = self.container.demux(self.selected_streams)

        if float(self.video_stream.average_rate)%1 != 0.0:
            logger.error('Videofile pts are not evenly spaced, pts to index conversion may fail and be inconsitent.')

        #load/generate timestamps.
        if timestamps is None:
            timestamps_path,ext =  os.path.splitext(source_path)
            timestamps = timestamps_path+'_timestamps.npy'
            try:
                self.timestamps = np.load(timestamps).tolist()
            except IOError:
                logger.warning("did not find timestamps file, making timetamps up based on fps and frame count. Frame count and timestamps are not accurate!")
                frame_rate = float(self.video_stream.average_rate)
                self.timestamps = [i/frame_rate for i in xrange(int(self.container.duration/av.time_base*frame_rate)+100)] # we are adding some slack.
            else:
                logger.debug("Auto loaded %s timestamps from %s"%(len(self.timestamps),timestamps))
        else:
            logger.debug('using timestamps from list')
            self.timestamps = timestamps
        self.next_frame = self._next_frame()

    @staticmethod
    def error_class():
        return FileCaptureError

    @property
    def frame_size(self):
        if self.video_stream:
            return int(self.video_stream.format.width),int(self.video_stream.format.height)
        else:
            logger.error("No videostream.")

    @property
    def frame_rate(self):
        return self.video_stream.average_rate

    @property
    def settings(self):
        settings = super(File_Source, self).settings
        settings['source_path'] = self.source_path
        settings['timestamps'] = self.timestamps
        return settings

    @settings.setter
    def settings(self,settings):
        pass

    def get_frame_index(self):
        return self.target_frame_idx

    def get_frame_count(self):
        return len(self.timestamps)

    def _next_frame(self):
        for packet in self.container.demux(self.video_stream):
            for frame in packet.decode():
                if frame:
                    yield frame
        raise EndofVideoFileError("end of file.")

    def pts_to_idx(self,pts):
        # some older mkv did not use perfect timestamping so we are doing int(round()) to clear that.
        # With properly spaced pts (any v0.6.100+ recording) just int() would suffice.
        # print float(pts*self.video_stream.time_base*self.video_stream.average_rate),round(pts*self.video_stream.time_base*self.video_stream.average_rate)
        return int(round(pts*self.video_stream.time_base*self.video_stream.average_rate))

    def pts_to_time(self,pts):
        ### we do not use this one, since we have our timestamps list.
        return int(pts*self.video_stream.time_base)

    def idx_to_pts(self,idx):
        return int(idx/self.video_stream.average_rate/self.video_stream.time_base)

    def get_frame_nowait(self):
        frame = None
        for frame in self.next_frame:
            index = self.pts_to_idx(frame.pts)
            if index == self.target_frame_idx:
                break
            elif index < self.target_frame_idx:
                pass
                # print 'skip frame to seek','now at:',index
            else:
                logger.error('Frame index not consistent.')
                break
        if not frame:
            raise EndofVideoFileError('Reached end of videofile')

        try:
            timestamp = self.timestamps[index]
        except IndexError:
            logger.warning("Reached end of timestamps list.")
            raise EndofVideoFileError("Reached end of timestamps list.")

        self.show_time = timestamp
        self.target_frame_idx = index+1
        return Frame(timestamp,frame,index=index)

    def wait(self,frame):
        if self.display_time:
            wait_time  = frame.timestamp - self.display_time - time()
            if 1 > wait_time > 0 :
                sleep(wait_time)
        self.display_time = frame.timestamp - time()
        sleep(self.slowdown)

    def get_frame(self):
        frame = self.get_frame_nowait()
        self.wait(frame)
        return frame

    def seek_to_frame(self, seek_pos):
        ###frame accurate seeking
        try:
            self.video_stream.seek(self.idx_to_pts(seek_pos),mode='time')
        except av.AVError as e:
            raise FileSeekError()
        else:
            self.next_frame = self._next_frame()
            self.display_time = 0
            self.target_frame_idx = seek_pos

    def seek_to_frame_fast(self, seek_pos):
        ###best effort seeking to closest keyframe
        self.video_stream.seek(self.idx_to_pts(seek_pos),mode='time')
        self.next_frame = self._next_frame()
        frame = self.next_frame.next()
        index = self.pts_to_idx(frame.pts)
        self.target_frame_idx = index+1
        self.display_time = 0


    def get_now(self):
        try:
            timestamp = self.timestamps[self.get_frame_index()-1]
            logger.debug("Filecapture is not a realtime source. -NOW- will be the current timestamp")
        except IndexError:
            logger.warning("timestamp not found.")
            timestamp = 0
        return timestamp

    def get_timestamp(self):
        return self.get_now()

    def init_gui(self):
        from pyglui import ui
        ui_elements = []
        ui_elements.append(ui.Info_Text("Running Capture with '%s' as src"%self.source_path))
        ui_elements.append(ui.Slider('slowdown',self,min=0,max=1.0))
        self.g_pool.capture_source_menu.extend(ui_elements)

    @property
    def jpeg_support(self):
        return False
