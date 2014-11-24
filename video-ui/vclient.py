#!/usr/bin/python
import cStringIO as StringIO
import errno
import functools
import json
import logging
import math
import optparse
import os
import socket
import subprocess
import sys
import textwrap
import time

import trollius as asyncio
from trollius import Task, From

from vui_helpers import (
    wrap_event, asyncio_misc_init, logging_init, MemoryLoggingHandler)
from video_window import VideoWindow, video_window_init, video_window_main

# must be after video_window
from gi.repository import GLib

import joystick
import legtool.async.trollius_trace
import linux_input
import calibration

# Possible locations for logdir. If any of the paths exist, will use this one.
# else, will use the last entry.
LOG_DIR_LOCATIONS = [
    '~/.mjmech-data',
    '~/mjmech-data',
    './mjmech-data' ]


def add_pair(a, b, scale=1.0):
    return (a[0] + b[0] * scale,
            a[1] + b[1] * scale)

RIPPLE_COMMAND = {
    'type': 'ripple',
    'translate_x_mm_s': 0,
    'translate_y_mm_s': 0,
    'rotate_deg_s': 0,
    }

IDLE_COMMAND = {
    'type': 'idle'
}

class UdpAnnounceReceiver(object):
    PORT = 13355

    def __init__(self, opts, logsaver=None):
        self.opts = opts
        self.logsaver = logsaver
        self.logger = logging.getLogger('announce-receiver')
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                  socket.IPPROTO_UDP)
        self.sock.setblocking(0)
        self.logger.info('Binding to port %d' % self.PORT)
        self.sock.bind(('', self.PORT))

        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        GLib.io_add_watch(self.sock.fileno(),
                          GLib.PRIORITY_DEFAULT,
                          GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
                          self._on_readable)
        self.logger.info('Waiting for address broadcast')
        self.last_ann_info = None
        self.last_run_info = None
        self.control = None
        self.deploying = False
        self.old_running_since = None

    @wrap_event
    def _on_readable(self, source, cond):
        while True:
            try:
                pkt, addr = self.sock.recvfrom(65535)
            except socket.error as e:
                if e.errno != errno.EAGAIN:
                    raise
                break
            data = json.loads(pkt)
            # Inject source address info
            data.update(addr = addr[0],
                        aport = addr[1]) # announce port
            self._on_server_packet(data)
        return True

    def _on_server_packet(self, data):
        ann_info = ('Launcher is at %s (port %s, hostname %r, start_time %s)'
                    ) % (
            data['addr'], data['aport'], data['host'],
            time.strftime('%F_%T', time.localtime(data['start_time'])))
        if ann_info != self.last_ann_info:
            self.logger.info(ann_info)
            self.last_ann_info = ann_info
        # TODO mafanasyev: assert IP did not change

        run_info = 'vserver '
        if data['running_since']:
            run_info += time.strftime('running since %F_%T',
                                      time.localtime(data['running_since']))
        else:
            run_info += 'not running'
        if run_info != self.last_run_info:
            self.logger.info(run_info)
            self.last_run_info = run_info

        if self.control is not None:
            return

        if not opts.no_upload:
            # Start deploy process, but only once
            if not self.deploying:
                # deploy will restart vserver. So do not start control interface
                # while old vserver is running
                self.old_running_since = data['running_since']
                self.deploying = True
                asyncio.Task(self._run_deploy_process(data['addr']))

            # Return if program is not running yet
            if data['running_since'] in [None, self.old_running_since]:
                return

        # Start UI now
        self.control = ControlInterface(
            self.opts, data['addr'], data.get('cport', None),
            logsaver=self.logsaver)

    @asyncio.coroutine
    @wrap_event
    def _run_deploy_process(self, addr):
        # subprocess_exec is broken in gbulb/base_events.py, so use shell

        deploy_cmd = "RHOST=%s %s start" % (
            'odroid@' + addr,
            os.path.join(
                os.path.dirname(__file__), 'deploy-vserver.sh'))
        logger = logging.getLogger('deploy')
        logger.warning('Running: %s', deploy_cmd)

        # We would like to capture stdout/stderr and redirect to logger, so that
        # we properly record deploy error messages, but gbulb seems to be
        # broken.
        proc = yield From(
            asyncio.create_subprocess_shell(
                deploy_cmd,
                close_fds=True,
                #stdin=open('/dev/stdin', 'r'),
                #stdout=asyncio.subprocess.PIPE,
                #stderr=asyncio.subprocess.STDOUT
                ))

        #while True:
        #    line = yield From(proc.stdout.readline())
        #    logger.info(repr(line))
        #    if line == '':
        #        break

        retcode = yield From(proc.wait())
        logger.warning('Process exited, code %r', retcode)


class ControlInterface(object):
    SEND_INTERVAL = 0.25
    DEFAULT_PORT = 13356
    VIDEO_PORT = 13357
    UI_STATE_SAVEFILE = "last.jsonlist"

    _INITIAL_UI_STATE = dict(
        # type marker used to recognize records in logfile. Do not change.
        _type = 'ui-state',
        # SVG is stretched to video after rendering, so small width/height
        # will make all lines thicker. Force to video resolution: 1920x1080
        image_size=(1920, 1080),
        reticle_on=True,
        reticle_offset=(0, 0),
        reticle_rotate=0,
        status_on=True,
        msg_font_size=20,
    )

    _INITIAL_CONTROL_DICT = dict(
        # type marker used to recognize records in logfile. Do not change.
        _type = 'control-dict',

        boot_time = time.time(),

        # Incremented on each packet TX
        seq = 0,

        # Set before each packet TX
        cli_time = None,

        video_port=VIDEO_PORT,
        # Not set: turret - pair of (x, y) degrees
        # Not set: gait - a dict based off IDLE/RIPPLE_COMMAND

        # 0 = off; 1 = on
        laser_on = 0,
        agitator_on = 0,
        green_led_on = 0,

        # pwm values in 0..1 range
        agitator_pwm = 0.5,
        fire_motor_pwm = 0.75,
        # fire command duration (seconds)
        fire_duration = 0.5,

        # fire a shot every time this changes; should only ever increase
        fire_cmd_count = 0,
        )


    def __init__(self, opts, host, port=None, logsaver=None):
        self.opts = opts
        self.logger = logging.getLogger('control')
        self.logsaver = logsaver
        self.asyncio_loop = asyncio.get_event_loop()
        self.addr = (host, port or self.DEFAULT_PORT)
        self.sock = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.c_cal = calibration.CameraCalibration()
        # TODO mafanasyev: load yaml here? Does it make sense if we do
        # not support distortion?

        self.sock.setblocking(0)
        self.logger.info('Connecting to %s:%d' % self.addr)
        self.sock.connect(self.addr)


        # Wallclock and pipeline time
        self.last_video_wall_time = None
        self.last_video_pp_time = None

        self.control_dict = dict(self._INITIAL_CONTROL_DICT)

        # If True, some gait-related key is being held.
        self.key_gait_active = False

        # state log file and last svg
        self.state_log_file = None
        self.last_svg_name = None
        self.state_savefile_name = None
        if opts.log_prefix:
            # Last svg needs no timestamp
            self.last_svg_name = os.path.join(
                os.path.dirname(opts.log_prefix + 'test'), 'last.svg')
            # Open file line-buffered
            self.state_log_file = open(
                opts.log_prefix + '.jsonlist', 'w', 1)
            # Prepare savefile name
            self.state_savefile_name = os.path.join(
                os.path.dirname(opts.log_prefix + 'test'),
                self.UI_STATE_SAVEFILE)

        # Control UI state. Should be a simple dict, as we will be serializing
        # it for later log replay.
        self.ui_state = dict(self._INITIAL_UI_STATE)
        self._logged_state = None

        # timestamp of last received remote log message
        # (set to 0 or None to disable received logs)
        self.remote_logs_from = 1

        # Most recent server state
        self.server_state = dict()

        restore_from = opts.restore_state

        # default -- only restore if file exists
        if (restore_from is None
            and self.state_savefile_name is not None
            and os.path.exists(self.state_savefile_name)):
            restore_from = self.state_savefile_name

        if restore_from:    # not empty string or None
            # Restore state
            logging.info('Loading saved state from %r', restore_from)
            with open(restore_from, 'r') as fh:
                restored = json.load(fh)
            self.ui_state.update(restored)

        self.video = None

        self._update_video_overlay_pending = False

        # Log state before video, to check for code errors in rendering function
        self._state_updated()

        if not opts.external_video:
            video_log = None
            if opts.log_prefix:
                video_log = opts.log_prefix + '.mkv'
            self.video = VideoWindow(host, self.VIDEO_PORT, video_log=video_log)
            self.video.on_video_click = self._handle_video_click
            self.video.on_key_press = self._handle_key_press
            self.video.on_key_release = self._handle_key_release
            self.video.on_got_video = functools.partial(
                self._state_updated, force=True)

        def metric(device):
            abs_axis = device.get_features(linux_input.EV.ABS)
            if len(abs_axis) >= 3:
                return 1

            return -1

        enumerator = joystick.JoystickEnumerator(metric)
        joysticks = enumerator.joysticks()
        if len(joysticks):
            self.joystick = joysticks[0]
            self.joystick_task = Task(self._read_joystick())
        else:
            self.joystick = None
            self.joystick_task = None
            self.logger.info("No joysticks found!")

        GLib.timeout_add(int(self.SEND_INTERVAL * 1000),
                         self._on_send_timer)
        GLib.io_add_watch(self.sock.fileno(),
                          GLib.PRIORITY_DEFAULT,
                          GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
                          self._on_readable)

        # Refresh overlay when new logs are arriving
        if self.logsaver:
            self.logsaver.on_record.append(
                lambda *_: self.update_video_overlay())


    def select_axes(self):
        complete = self.joystick.get_features(linux_input.EV.ABS)

        # If we have all of X, Y, and RX, RY, then the axes are
        # probably mapped correctly and we can return what we expect.
        if (linux_input.ABS.X in complete and
            linux_input.ABS.Y in complete and
            linux_input.ABS.RX in complete and
            linux_input.ABS.RY in complete):
            return [linux_input.ABS.X, linux_input.ABS.Y, linux_input.ABS.RX]

        # Otherwise, just return the first three axes on the hope that
        # this is meaningful.
        return complete[0:3]

    @wrap_event
    def _read_joystick(self):
        axes = self.select_axes()
        print "Selected joystick axes:", axes

        while True:
            ev = yield From(self.joystick.read())

            if ev.ev_type != linux_input.EV.ABS:
                continue

            dx = self.joystick.absinfo(axes[0]).scaled()
            dy = self.joystick.absinfo(axes[1]).scaled()
            dr = self.joystick.absinfo(axes[2]).scaled()

            if abs(dx) < 0.2 and abs(dy) < 0.2 and abs(dr) < 0.2:
                if self.control_dict.get('gait') is not None:
                    self.control_dict['gait'] = IDLE_COMMAND
            else:
                if abs(dy) > 0.1:
                    dx = 0.0

                gait = RIPPLE_COMMAND.copy()
                gait['translate_x_mm_s'] = dx * 40
                gait['translate_y_mm_s'] = -dy * 100
                gait['rotate_deg_s'] = dr * 50
                self.control_dict['gait'] = gait

    @wrap_event
    def _on_send_timer(self):
        # Raise exception if any of tasks terminate
        if self.joystick_task and self.joystick_task.done():
            #self.joystick_task.result()
            self.joystick_task = None

        self._send_control()

        if self.video:
            tm_pp = self.video.get_time()
            tm_wc = time.time()
            if self.last_video_pp_time is not None:
                dt_pp = tm_pp - self.last_video_pp_time
                dt_wc = tm_wc - self.last_video_wall_time
                lost = (dt_wc - dt_pp)
                if abs(lost) > 0.01:
                    self.logger.info('Video time lost: %.3f msec (%.1f%%)' % (
                        lost * 1000.0, lost * 100.0 / (dt_wc or 1)))

            self.last_video_wall_time = tm_wc
            self.last_video_pp_time = tm_pp
        return True

    @wrap_event
    def _on_readable(self, source, cond):
        while True:
            try:
                pkt, addr = self.sock.recvfrom(65535)
            except socket.error as e:
                if e.errno != errno.EAGAIN:
                    self.logger.error('Cannot read response: %s', e)
                break
            assert addr == self.addr, \
                'Got packet from %r, expected from %r' % (addr, self.addr)
            self._handle_packet(pkt)
        return True

    def _send_control(self):
        self.control_dict['seq'] += 1
        self.control_dict.update(
            logs_from=self.remote_logs_from,
            cli_time=time.time())
        serialized = self._log_struct(self.control_dict)
        self.sock.send(serialized)
        if self.control_dict['seq'] == 3 and self.video:
            self.video.start()

    def _handle_packet(self, pkt_raw):
        # Get the structure
        pkt = json.loads(pkt_raw)

        # Prepare to log
        pkt.update(cli_time=time.time(),
                   _type='srv-state')

        # Parse out messages -- no need to log them twice
        logs = pkt.pop('logs_data', None) or []
        for entry in logs:
            if entry[0] > self.remote_logs_from:
                # new record
                self.remote_logs_from = entry[0]
                log_dict = MemoryLoggingHandler.to_dict(
                    entry, time_field='srv_time')
                log_dict.update(
                    _type='srv-log', cli_time=pkt['cli_time'])
                self._log_struct(log_dict)
                MemoryLoggingHandler.relog(entry, prefix='srv.')

        # Log it and store (for display)
        self._log_struct(pkt)
        self.server_state = pkt
        self.update_video_overlay()


    def _handle_video_click(self, pos, button, moved=False):
        """User clicked button 1 on video window.
        pos is (x, y) tuple in 0..1 range"""
        assert len(pos) == 2
        if moved:
            # Only care about initial click.
            return

        # This image_size will be broken if aspect ratio is different -- this
        # usually means truncated image, not resized pixels; but we do not care
        # for now.
        w2d = self.c_cal.to_world2d(pos, image_size=(1.0, 1.0))
        w2ang = (
             math.degrees(math.atan(w2d[0])),
             math.degrees(math.atan(w2d[1]))
             )

        if button != 1:
            self.logger.info('Click with B%d at (w2d %.3f,%.3f, w2a '
                             '%.1f,%.1f)' % ((button, ) + w2d + w2ang))
            return

        old_turret = self.control_dict.get('turret')
        if old_turret is None:
            self.logger.warn('Cannot move turret -- center it first '
                             '(w2d %.3f,%.3f, w2a %.1f,%.1f)' % (w2d + w2ang))
            return

        ang_x, ang_y = old_turret
        ang_x += w2ang[0]
        ang_y += w2ang[1]
        # TODO mafanasyev: add reticle_rotate support

        self.logger.debug('Moving turret to (%+.1f, %+.1f) deg in response '
                          'to click at (%+.4f, %+.4f)',
                         ang_x, ang_y, w2d[0], w2d[1])
        self.control_dict['turret'] = (ang_x, ang_y)
        self._send_control()

    _GAIT_KEYS = {  #  key->(dx,dy,dr)
        'w': (0, 1, 0),
        's': (0, -1, 0),
        'a': (-1, 0, 0),
        'd': (1, 0, 0),
        'q': (0, 0, -1),
        'e': (0, 0, 1)
    }

    _ARROW_KEYS = { # key->(dx, dy)
        'Left': (-1, 0),
        'Right': (1, 0),
        'Up': (0, -1),
        'Down': (0, 1)
    }

    def _handle_key_press(self, base_name, modifiers):
        arrows = self._ARROW_KEYS.get(base_name)
        if arrows:
            base_name = 'Arrows'

        name = modifiers + base_name

        if name in self._GAIT_KEYS:
            dx, dy, dr = self._GAIT_KEYS[name]
            gait = RIPPLE_COMMAND.copy()
            gait['translate_x_mm_s'] = dx * 50
            gait['translate_y_mm_s'] = dy * 100
            gait['rotate_deg_s'] = dr * 30
            self.control_dict['gait'] = gait
            self.key_gait_active = True
        elif name == 'h':
            self._print_help()
        elif name == 'l':
            self.control_dict['laser_on'] ^= 1
            self.logger.info('Laser set to %d',
                             self.control_dict['laser_on'])
        elif name == 'm':
            self.control_dict['agitator_on'] ^= 1
            self.logger.info('Agitator set to %d (pwm %.3f)',
                             self.control_dict['agitator_on'],
                             self.control_dict['agitator_pwm'])
        elif name == 'S-G':
            self.control_dict['green_led_on'] ^= 1
            self.logger.info('Green LED set to %d',
                             self.control_dict['green_led_on'])
        elif name in ['Return']:
            self.control_dict['fire_cmd_count'] += 1
            self.logger.info('Sent fire command')
        elif name == 'c':
            self.control_dict['turret'] = (0.0, 0.0)
            self.logger.info('Centered turret')
        elif name in ['C-Arrows', 'C-S-Arrows']:
            # Ctrl + arrows to move reticle center
            step = 0.010 if ('S-' in modifiers) else 0.002
            self.ui_state['reticle_offset'] = add_pair(
                self.ui_state['reticle_offset'], arrows, step)
        elif name in ['Arrows', 'S-Arrows']:
            # Move turret
            step = 5.0 if ('S-' in modifiers) else 0.5
            if self.control_dict.get('turret') is None:
                self.logger.warn('Cannot move turret -- center it first')
            else:
                self.control_dict['turret'] = add_pair(
                    self.control_dict['turret'], arrows, step)
        elif name == 'r':
            self.ui_state['reticle_on'] ^= True
            self.logger.info('Set reticle_on=%r', self.ui_state['reticle_on'])
        elif name == 'KP_Add':
            self.ui_state['msg_font_size'] += 1
        elif name == 'KP_Subtract':
            self.ui_state['msg_font_size'] = max(
                4, self.ui_state['msg_font_size'] - 1)
        elif name == 'Escape':
            if self.logsaver and self.logsaver.data:
                self.logger.info('Cleared on-screen display (%d lines)',
                                 len(self.logsaver.data))
                # TODO mafanasyev: when we will log logs properly, we should
                # also log 'clear' events, so we can reconstruct OSD properly
                del self.logsaver.data[:]
            else:
                self.ui_state['status_on'] ^= True
                self.logger.debug('Set status_on=%r',
                                  self.ui_state['status_on'])
        elif base_name in ['Shift_R', 'Shift_L',
                           'Control_R', 'Control_L',
                           'Alt_R', 'Alt_L']:
            # Ignore modifiers
            pass
        else:
            self.logger.debug('Unknown key %r' % name)

        self._state_updated()
        self._send_control()

    def _handle_key_release(self, evt):
        if self.key_gait_active:
            self.key_gait_active = False
            if self.control_dict['gait'] is not None:
                self.control_dict['gait'] = IDLE_COMMAND
            self._send_control()

    def _print_help(self):
        helpmsg = """
        Help on Keys:
          w/s, a/d - move
          q/e      - rotate
          l        - laser on/off
          m        - agitator on/off
          G        - green LED on/off
          c        - enable && center turret
          click    - point turret to this location (must center first)
          ENTER    - fire
          arrows   - move turret
          S+arrows - move turret (move faster)
          C+arrows - set reticle center (use shift for more precision)
          C+S+arrows - set reticle center (move faster)
          r        - toggle reticl
          ESC      - clear logs from OSD; then toggle servo status
          Numpad +/-  - change OSD font size
        """
        for line in textwrap.dedent(helpmsg).strip().splitlines():
            self.logger.info('| ' + line)

    def _log_struct(self, rec):
        """Log @p rec (which must be a json-serialized dict) to logfile.
        @p rec must contain fields '_type' and 'cli_time'
        @returns serialized string
        """
        sertext = json.dumps(rec, sort_keys=True)
        if self.state_log_file:
            print >>self.state_log_file, sertext
            # No need to flush, file should have autoflush

        return sertext

    def _state_updated(self, force=False):
        sertext = json.dumps(self.ui_state, sort_keys=True)
        if (sertext == self._logged_state) and not force:
            return

        # TODO mafanasyev: add video timestamp
        self.ui_state['cli_time'] = time.time()
        sertext = self._log_struct(self.ui_state)

        self._logged_text = sertext

        if self.state_savefile_name is not None:
            with open(self.state_savefile_name + '~', 'w') as fh:
                print >>fh, sertext
            # Atomically install new version
            os.rename(self.state_savefile_name + '~',
                      self.state_savefile_name)
        self.update_video_overlay()

    def update_video_overlay(self):
        """Request update of video overlay in thread-safe way"""
        if self._update_video_overlay_pending:
            # An update is pending
            return
        self._update_video_overlay_pending = True
        self.asyncio_loop.call_soon_threadsafe(self._update_video_overlay_now)

    @wrap_event
    def _update_video_overlay_now(self):
        self._update_video_overlay_pending = False

        if self.logsaver:
            logs = self.logsaver.data
        else:
            logs = []
        stream = StringIO.StringIO()
        self._render_svg(stream, self.ui_state, self.control_dict,
                         self.server_state, logs)
        if self.video:
            self.video.set_svg_overlay(stream.getvalue())

        if self.last_svg_name:
            with open(self.last_svg_name + '~', 'w') as f:
                f.write(stream.getvalue())
            os.rename(self.last_svg_name + '~', self.last_svg_name)

    @staticmethod
    def _render_svg(out, ui_state, control_dict, server_state, logs):
        """Render SVG for a given state. Should not access anything other
        than parameters, as this function may be called during replay.
        """
        print >>out, '<svg width="{image_size[0]}" height="{image_size[1]}">'\
            .format(**ui_state)

        if ui_state['reticle_on']:
            reticle_center_rel = add_pair((0.5, 0.5),
                                          ui_state['reticle_offset'])
            reticle_center = (
                ui_state['image_size'][0] * reticle_center_rel[0],
                ui_state['image_size'][1] * reticle_center_rel[1])

            print >>out, '''
<g transform='rotate({0[reticle_rotate]}) translate({1[0]} {1[1]})'
   stroke="rgb(255,128,0)">
  <line x1="500"  x2="100"  y1="0" y2="0" stroke-width="4" />
  <line x1="-500" x2="-100" y1="0" y2="0" stroke-width="4" />
  <line x1="-100" x2="100"  y1="0" y2="0" />
  <line y1="500"  y2="100"  x1="0" x2="0" stroke-width="4" />
  <line y1="-500" y2="-100" x1="0" x2="0" stroke-width="4" />
  <line y1="-100" y2="100"  x1="0" x2="0" />

  <line x1="-80" x2="80"  y1="-20" y2="-20" />
  <line x1="-80" x2="80"  y1="20" y2="20" />
  <line x1="-60" x2="60"  y1="40" y2="40" />
  <line x1="-40" x2="40"  y1="60" y2="60" />
  <line x1="-20" x2="20"  y1="80" y2="80" />
</g>
'''.format(ui_state, reticle_center)

        status_lines = list()
        if not ui_state['status_on']:
            status_lines.append('[OFF]')
        else:
            # Add turret position
            if control_dict.get('turret') is None:
                status_lines.append('Turret OFF')
            else:
                status_lines.append('Turret: ({:+5.1f}, {:+5.1f})'
                                    .format(*control_dict['turret']))

            # Add GPIO status
            tags = list()
            if control_dict['laser_on']:
                tags.append('LAS')
            if control_dict['agitator_on']:
                tags.append('AGT')
            if control_dict['green_led_on']:
                tags.append('GRN')
            if not tags:
                tags.append('(all off)')
            status_lines.append(','.join(tags))

            # Add servo status
            if server_state:
                s_voltage = server_state.get('servo_voltage', {})
                s_status = server_state.get('servo_status', {})
            else:
                s_voltage = s_status = {}
            got_any = False
            for servo in sorted(set(s_voltage.keys() + s_status.keys()),
                                key=int):
                tags = []
                if s_status.get(servo):
                    tags.append(str(s_status[servo]))
                if s_voltage.get(servo):
                    tags.append('%.2fV' % s_voltage[servo])
                if not tags:
                    continue
                tags.append("%-2s" % servo)
                status_lines.append(' '.join(tags))
                got_any = True
            if not got_any:
                status_lines.append('No servo status available')

        # We output each text twice: first text outline in black, then text
        # itself in bright color. This ensures good visibility over both black
        # and green background.
        for tp in ['stroke="black" fill="black"', 'fill="COLOR"']:
            print >>out, '''
<text transform="translate(10 {0})" {1}
   font-family="Helvetica,sans-serif"
   font-size="{2}" text-anchor="left" dominant-baseline="text-before-edge">
'''.format(ui_state['image_size'][1] - 15,
           tp.replace('COLOR', 'lime'), ui_state['msg_font_size'])
            # Total number of lines is specified in MemoryLoggingHandler
            # constructor.
            for line_num, mtuple in enumerate(reversed(logs)):
                line = MemoryLoggingHandler.to_string(mtuple)
                print >>out, '<tspan x="0" y="%d"><![CDATA[%s]]></tspan>' % (
                    (-1 - line_num) * ui_state['msg_font_size'], line)
            print >>out, '</text>'

            print >>out, '''
<text transform="translate({0} 15)" {1}
   font-family="Courier,fixed" font-weight="bold"
   font-size="{2}" text-anchor="end" dominant-baseline="text-before-edge">
'''.format(ui_state['image_size'][0] - 10, tp.replace('COLOR', 'white'),
           ui_state['msg_font_size'])
            for line_num, line in enumerate(status_lines):
                print >>out, ('<tspan x="0" y="%d"><![CDATA[%s]]></tspan>'
                              % (line_num * ui_state['msg_font_size'], line))
            print >>out, '</text>'

        print >>out, '</svg>'


def main(opts):
    asyncio_misc_init()

    logging_init(verbose=True)
    logsaver = MemoryLoggingHandler(install=True, max_records=30)

    root = logging.getLogger()

    if opts.log_dir is None:
        for pdir in LOG_DIR_LOCATIONS:
            opts.log_dir = os.path.abspath(os.path.expanduser(pdir))
            if os.path.exists(opts.log_dir):
                break
        logging.info('Auto-detected logdir as %r', opts.log_dir)

    if opts.check:
        logging.info('Check passed')
        return

    if opts.log_dir != '' and opts.log_prefix is None:
        opts.log_prefix = os.path.abspath(os.path.join(
            opts.log_dir,
            time.strftime('mjlog-%Y%m%d-%H%M%S', time.localtime())))

    if opts.log_prefix:
        print 'Saving logs to %s.*' % opts.log_prefix
        txtname = opts.log_prefix + '.log'
        try:
            os.makedirs(os.path.dirname(txtname))
        except OSError:
            pass

        txtlog = logging.FileHandler(txtname, encoding='utf-8')
        txtlog.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs).3d [%(levelname).1s] %(name)s: %(message)s",
            datefmt="%F %T"))
        txtlog.setLevel(logging.DEBUG)
        root.addHandler(txtlog)


    video_window_init()

    if opts.addr is None:
        ann = UdpAnnounceReceiver(opts, logsaver=logsaver)
    else:
        cif = ControlInterface(opts, opts.addr, logsaver=logsaver)

    logging.info('Running')
    video_window_main()

if __name__ == '__main__':
    parser = optparse.OptionParser()
    parser.add_option('--check', action='store_true',
                      help='Exit immediately after loading program')
    parser.add_option('--addr', metavar='IP',
                      help='Client address (default autodiscover, explicit '
                      'address disables code upload)')
    parser.add_option('--no-upload', action='store_true',
                      help='Do not upload/run server code on odroid')
    parser.add_option('--external-video', action='store_true',
                      help='Use external process for video playing')
    parser.add_option('--log-prefix', default=None,
                      help='Write logfiles with specified prefix '
                      '(must include path, default base on log-dir and '
                      'current timestamp)')
    parser.add_option('-L', '--log-dir',
                      help='Write logfiles using autogenerated names in '
                      'this directory. Empty to disable. Default autodetect.')
    parser.add_option('-R', '--restore-state', default=None,
                      help='Restore UI state on startup. Default LOGDIR/' +
                      ControlInterface.UI_STATE_SAVEFILE)

    opts, args = parser.parse_args()
    if len(args) and opts.addr is None:
        opts.addr = args.pop(0)
    if args:
        parser.error('Invalid number of args')

    main(opts)
