from enum import Enum


class Axle(str, Enum):
    FRONT = "FRONT"
    REAR = "REAR"
    BOTH = "BOTH"
    UNKNOWN = "UNKNOWN"
