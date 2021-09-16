#!/usr/bin/env python3

import RPi.GPIO as gpio
from evdev import InputDevice, KeyEvent, ecodes

import os
import sys
import time
import random
import threading
from subprocess import Popen

VALID_VIDEO_TYPES = ['.mp4', '.mkv']
DATA_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'data')
BUTTON_GPIO = 26
TOUCHSCREEN_DEVICE_PATH = '/dev/input/event0'
DOUBLE_CLICK_THRESHOLD_SEC = 0.20
LONG_PRESS_THRESHOLD_SEC = 2.0


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


def play_videos(videos):
    random.shuffle(videos)
    for video in videos:
        print("Playing video %s" % video)
        playProcess = Popen(['omxplayer', '--no-osd', '--aspect-mode', 'fill', video])
        playProcess.wait()


def video_loop():
    shows = os.listdir(DATA_DIR)
    random.shuffle(shows)
    print("Playing show... %s!" % shows[0])
    videos = get_videos(os.path.join(DATA_DIR, shows[0]))
    while (True):
        play_videos(videos)


def touchscreen_loop():
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
                if key_up_diff < DOUBLE_CLICK_THRESHOLD_SEC:
                    print("Double click!")
                elif key_down_diff > LONG_PRESS_THRESHOLD_SEC:
                    print("Long press!")

            last_event_time[event.value] = new_event_time[event.value]


def main():
    # Initialize GPIOs to allow turning the screen on/off and detecting button presses
    os.system('raspi-gpio set 19 ip')
    gpio.setwarnings(False)
    gpio.setmode(gpio.BCM)
    gpio.setup(BUTTON_GPIO, gpio.IN, pull_up_down=gpio.PUD_UP)
    gpio.setup(18, gpio.OUT)

    # Configure the button callback (which starts its own thread)
    configure_button_callback()

    # Kick off the video player thread
    player_thread = threading.Thread(target=video_loop, daemon=True)
    player_thread.start()

    # And the touchscreen event thread
    touchscreen_thread = threading.Thread(target=touchscreen_loop, daemon=True)
    touchscreen_thread.start()

    # Run forever
    player_thread.join()
    touchscreen_thread.join()


if __name__ == '__main__':
    main()
