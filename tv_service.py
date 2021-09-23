#!/usr/bin/env python3

import RPi.GPIO as gpio
from evdev import InputDevice, KeyEvent, ecodes

import os
import sys
import time
import random
import psutil
import signal
import threading
from enum import Enum
from queue import Queue, Empty
from subprocess import Popen

VALID_VIDEO_TYPES = ['.mp4', '.mkv']
DATA_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'data')
BUTTON_GPIO = 26
TOUCHSCREEN_DEVICE_PATH = '/dev/input/event0'
DOUBLE_CLICK_THRESHOLD_SEC = 0.20
LONG_PRESS_THRESHOLD_SEC = 2.0
TV_STATIC_FILENAME = 'tv_static.mp4'
INITIAL_TV_STATIC_DURATION_SEC = 1.5


class TouchScreenCommand(Enum):
    SKIP = 1
    CHANGE_SHOW = 2


class ButtonHandler(threading.Thread):
    """
    Standard edge event handlers will only get the first event detected, which may not
    properly represent the final state of the switch. This class gives the switch time
    to settle and reflects the final state of the switch in the callback.

    Taken from: https://raspberrypi.stackexchange.com/a/76738
    """
    def __init__(self, pin, func, edge='both', bouncetime=200):
        super().__init__(daemon=True)

        self.edge = edge
        self.func = func
        self.pin = pin
        self.bouncetime = float(bouncetime) / 1000

        self.lastpinval = gpio.input(self.pin)
        self.lock = threading.Lock()

    def __call__(self, *args):
        if not self.lock.acquire(blocking=False):
            return

        t = threading.Timer(self.bouncetime, self.read, args=args)
        t.start()

    def read(self, *args):
        pinval = gpio.input(self.pin)

        if (
                ((pinval == 0 and self.lastpinval == 1) and
                 (self.edge in ['falling', 'both'])) or
                ((pinval == 1 and self.lastpinval == 0) and
                 (self.edge in ['rising', 'both']))
        ):
            self.func(*args)

        self.lastpinval = pinval
        self.lock.release()


def turn_on_screen():
    print("Turning on screen.")
    os.system('raspi-gpio set 19 op a5')
    gpio.output(18, gpio.HIGH)


def turn_off_screen():
    print("Turning off screen.")
    os.system('raspi-gpio set 19 ip')
    gpio.output(18, gpio.LOW)


def button_callback(channel):
    button_pressed = gpio.input(channel)
    if button_pressed:
        turn_on_screen()
    else:
        turn_off_screen()


def configure_button_callback():
    handler = ButtonHandler(BUTTON_GPIO, button_callback, edge='both', bouncetime=100)
    try:
        gpio.add_event_detect(BUTTON_GPIO, gpio.BOTH, callback=handler)
    except RuntimeError:
        print("Failed to add edge detection, try running the script as root.")
        sys.exit(-1)

    button_callback(BUTTON_GPIO)  # Set initial screen on/off state


def get_videos(directory):
    videos = []
    for file in os.listdir(directory):
        if any([file.lower().endswith(vtype) for vtype in VALID_VIDEO_TYPES]):
            videos.append(os.path.join(directory, file))
    print("Found %d videos in directory %s" % (len(videos), directory))
    return videos


def kill_child_processes(parent_pid, sig=signal.SIGTERM):
    try:
        parent = psutil.Process(parent_pid)
    except psutil.NoSuchProcess:
        print("No such process %d" % parent_pid)
        return
    children = parent.children(recursive=True)
    for process in children:
        print("Sending signal %s to process %d" % (sig, process.pid))
        process.send_signal(sig)


def resume_tv_static(tv_static_proc):
    if tv_static_proc:
        kill_child_processes(tv_static_proc.pid, signal.SIGCONT)


def stop_tv_static(tv_static_proc):
    if tv_static_proc:
        kill_child_processes(tv_static_proc.pid, signal.SIGSTOP)


def play_videos(videos, command_queue, tv_static_proc):
    random.shuffle(videos)
    for video in videos:
        print("Playing video %s" % video)
        stop_tv_static(tv_static_proc)
        play_process = Popen(['omxplayer', '--no-osd', '--aspect-mode', 'fill', video])
        while play_process.poll() is None:
            try:
                command = command_queue.get(timeout=1)
            except Empty:
                continue

            print("Received a %s" % command)
            # Regardless if we're skipping the current video or changing shows,
            # the play_process needs to be killed. omxplayer does its real
            # work in a child process it spawns, so it needs to be killed as well
            resume_tv_static(tv_static_proc)
            kill_child_processes(play_process.pid)
            play_process.kill()

            if command == TouchScreenCommand.SKIP:
                # Play the next video in the videos list
                break
            elif command == TouchScreenCommand.CHANGE_SHOW:
                # Return to video_loop() and select a new show to play
                return

        play_process.wait()


def video_loop(command_queue, show_to_start_with=None, tv_static_proc=None):
    last_show_played = None
    while (True):
        shows = [s for s in os.listdir(DATA_DIR) if os.path.isdir(os.path.join(DATA_DIR, s))]
        if show_to_start_with:
            if show_to_start_with not in shows:
                print("Show %s was requested to start playing," % show_to_start_with,
                      "but is not one of the available shows: %s" % shows)
                sys.exit(-1)
            else:
                show_to_play = last_show_played = show_to_start_with
                show_to_start_with = None
        else:
            candidate_shows = list(shows)
            if last_show_played:
                candidate_shows.remove(last_show_played)
            random.shuffle(candidate_shows)
            show_to_play = candidate_shows[0]
        print("Playing show... %s!" % show_to_play)
        videos = get_videos(os.path.join(DATA_DIR, show_to_play))
        last_show_played = show_to_play
        play_videos(videos, command_queue, tv_static_proc)


def touchscreen_loop(command_queue):
    dev = InputDevice(TOUCHSCREEN_DEVICE_PATH)

    last_event_time = {
        KeyEvent.key_up: time.time(),
        KeyEvent.key_down: time.time()
    }

    new_event_time = dict(last_event_time)

    for event in dev.read_loop():
        if event.type == ecodes.EV_KEY:
            new_event_time[event.value] = time.time()

            if event.value == KeyEvent.key_up:
                key_up_diff = new_event_time[event.value] - last_event_time[event.value]
                key_down_diff = new_event_time[event.value] - last_event_time[KeyEvent.key_down]

                # If the user double-clicked, skip the episode.
                # If the user long-pressed the screen, change the show.
                if key_up_diff < DOUBLE_CLICK_THRESHOLD_SEC:
                    command_queue.put(TouchScreenCommand.SKIP)
                elif key_down_diff > LONG_PRESS_THRESHOLD_SEC:
                    command_queue.put(TouchScreenCommand.CHANGE_SHOW)

            last_event_time[event.value] = new_event_time[event.value]


def main():
    # Initialize GPIOs to allow turning the screen on/off and detecting button presses
    os.system('raspi-gpio set 19 ip')
    gpio.setwarnings(False)
    gpio.setmode(gpio.BCM)
    gpio.setup(BUTTON_GPIO, gpio.IN, pull_up_down=gpio.PUD_UP)
    gpio.setup(18, gpio.OUT)

    # Create an omxplayer process to play TV static. We'll pause/resume this process
    # with SIGSTOP/SIGCONT signals between videos instead of just having a blank screen
    tv_static_filepath = os.path.join(DATA_DIR, TV_STATIC_FILENAME)
    tv_static_proc = None
    if os.path.exists(tv_static_filepath):
        tv_static_proc = Popen(['omxplayer', '--no-osd', '--loop', tv_static_filepath])
        time.sleep(INITIAL_TV_STATIC_DURATION_SEC)  # Sleep a little bit to show the effect on startup

    # Configure the button callback (which starts its own thread)
    configure_button_callback()

    # Queue to be used for the touchscreen thread to send events to the player thread
    command_queue = Queue()

    # Kick off the video player thread with the desired show (if specified)
    if len(sys.argv) == 2:
        show_to_start_with = sys.argv[1]
    else:
        show_to_start_with = None
    player_thread = threading.Thread(target=video_loop, args=(command_queue, show_to_start_with, tv_static_proc), daemon=True)
    player_thread.start()

    # And the touchscreen event thread
    touchscreen_thread = threading.Thread(target=touchscreen_loop, args=(command_queue,), daemon=True)
    touchscreen_thread.start()

    # Run forever. Purposefully not .join()ing the touchscreen thread, so if there is an
    # error in the player thread, the application will exit and get restarted by systemd
    player_thread.join()


if __name__ == '__main__':
    main()
