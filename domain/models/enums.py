from enum import Enum


class Axle(str, Enum):
    FRONT = "FRONT"
    REAR = "REAR"
    BOTH = "BOTH"
    UNKNOWN = "UNKNOWN"


class Side(str, Enum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    BOTH = "BOTH"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"
