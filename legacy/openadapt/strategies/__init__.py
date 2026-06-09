"""Package containing different replay strategies.

Module: __init__.py
"""

# flake8: noqa

from legacy.openadapt.strategies.base import BaseReplayStrategy
from legacy.openadapt.strategies.visual_browser import VisualBrowserReplayStrategy

# disabled because importing is expensive
# from legacy.openadapt.strategies.demo import DemoReplayStrategy
from legacy.openadapt.strategies.naive import NaiveReplayStrategy
from legacy.openadapt.strategies.segment import SegmentReplayStrategy
from legacy.openadapt.strategies.stateful import StatefulReplayStrategy
from legacy.openadapt.strategies.vanilla import VanillaReplayStrategy
from legacy.openadapt.strategies.visual import VisualReplayStrategy

# add more strategies here
