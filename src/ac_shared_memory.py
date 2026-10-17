"""Assetto Corsa shared-memory layouts and status constants.

Keep these definitions free of mapping side effects so readers can choose their
own mapping lifetime.
"""
import ctypes
from ctypes import c_float, c_int32, c_wchar


AC_STATUS = c_int32
AC_OFF = 0
AC_REPLAY = 1
AC_LIVE = 2
AC_PAUSE = 3
AC_SESSION_TYPE = c_int32
AC_UNKNOWN = -1
AC_PRACTICE = 0
AC_QUALIFY = 1
AC_RACE = 2
AC_HOTLAP = 3
AC_TIME_ATTACK = 4
AC_DRIFT = 5
AC_DRAG = 6

AC_FLAG_TYPE = c_int32
AC_NO_FLAG = 0
AC_BLUE_FLAG = 1
AC_YELLOW_FLAG = 2
AC_BLACK_FLAG = 3
AC_WHITE_FLAG = 4
AC_CHECKERED_FLAG = 5
AC_PENALTY_FLAG = 6


class SPageFilePhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('packetId', c_int32), ('gas', c_float), ('brake', c_float),
        ('fuel', c_float), ('gear', c_int32), ('rpms', c_int32),
        ('steerAngle', c_float), ('speedKmh', c_float),
        ('velocity', c_float * 3), ('accG', c_float * 3),
        ('wheelSlip', c_float * 4), ('wheelLoad', c_float * 4),
        ('wheelsPressure', c_float * 4), ('wheelAngularSpeed', c_float * 4),
        ('tyreWear', c_float * 4), ('tyreDirtyLevel', c_float * 4),
        ('tyreCoreTemperature', c_float * 4), ('camberRAD', c_float * 4),
        ('suspensionTravel', c_float * 4), ('drs', c_float), ('tc', c_float),
        ('heading', c_float), ('pitch', c_float), ('roll', c_float),
        ('cgHeight', c_float), ('carDamage', c_float * 5),
        ('numberOfTyresOut', c_int32), ('pitLimiterOn', c_int32),
        ('abs', c_float), ('kersCharge', c_float), ('kersInput', c_float),
        ('autoShifterOn', c_int32), ('rideHeight', c_float * 2),
    ]


class SPageFileGraphic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('packetId', c_int32), ('status', AC_STATUS),
        ('session', AC_SESSION_TYPE), ('currentTime', c_wchar * 15),
        ('lastTime', c_wchar * 15), ('bestTime', c_wchar * 15),
        ('split', c_wchar * 15), ('completedLaps', c_int32),
        ('position', c_int32), ('iCurrentTime', c_int32),
        ('iLastTime', c_int32), ('iBestTime', c_int32),
        ('sessionTimeLeft', c_float), ('distanceTraveled', c_float),
        ('isInPit', c_int32), ('currentSectorIndex', c_int32),
        ('lastSectorTime', c_int32), ('numberOfLaps', c_int32),
        ('tyreCompound', c_wchar * 33), ('replayTimeMultiplier', c_float),
        ('normalizedCarPosition', c_float), ('carCoordinates', c_float * 3),
        ('penaltyTime', c_float), ('flag', AC_FLAG_TYPE),
        ('idealLineOn', c_int32),
        ('isInPitLane', c_int32), ('surfaceGrip', c_float),
        ('mandatoryPitDone', c_int32), ('windSpeed', c_float),
        ('windDirection', c_float),
    ]


class SPageFileStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('_smVersion', c_wchar * 15), ('_acVersion', c_wchar * 15),
        ('numberOfSessions', c_int32), ('numCars', c_int32),
        ('carModel', c_wchar * 33), ('track', c_wchar * 33),
        ('playerName', c_wchar * 33), ('playerSurname', c_wchar * 33),
        ('playerNick', c_wchar * 33), ('sectorCount', c_int32),
        ('maxTorque', c_float), ('maxPower', c_float), ('maxRpm', c_int32),
        ('maxFuel', c_float), ('suspensionMaxTravel', c_float * 4),
        ('tyreRadius', c_float * 4),
    ]
