"""Предохранитель: съёмка экрана не идёт, пока запущен античит.

Решение студента (2026-11-23, docs/screen.md): распознавание экрана — только в одиночных
играх без античита. Издатели игр с античитом могут запретить любую стороннюю программу
своими правилами, и бан приходит спустя дни, а не сразу. Предохранитель не даёт включить
съёмку по ошибке, когда рядом открыт, скажем, Escape from Tarkov: при запущенном античите
наблюдатель не стартует, а запущенный — останавливается и объясняет почему.

Проверка читает только имена запущенных процессов, тем же способом, что Диспетчер задач
или определение игры в Discord и Steam: снимок списка процессов системы, без открытия
самих процессов и без доступа к их памяти.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Iterable, List, Optional

from vision.capture import IS_WINDOWS

# Процессы античитов: имя в нижнем регистре → название. Список заведомо неполный: в него
# входят распространённые системы, которые ставятся вместе с онлайн-играми.
ANTICHEAT_PROCESSES = {
    "beservice.exe": "BattlEye",
    "beservice_x64.exe": "BattlEye",
    "easyanticheat.exe": "Easy Anti-Cheat",
    "easyanticheat_eos.exe": "Easy Anti-Cheat",
    "eac_launcher.exe": "Easy Anti-Cheat",
    "start_protected_game.exe": "Easy Anti-Cheat",
    "vgc.exe": "Riot Vanguard",
    "vgtray.exe": "Riot Vanguard",
    "faceit.exe": "FACEIT",
    "faceitservice.exe": "FACEIT",
    "eseaclient.exe": "ESEA",
    "pnkbstra.exe": "PunkBuster",
    "pnkbstrb.exe": "PunkBuster",
    "gamemon.des": "nProtect GameGuard",
    "gamemon64.des": "nProtect GameGuard",
}

# Игры с BattlEye запускаются через исполняемый файл с окончанием «_BE»:
# EscapeFromTarkov_BE.exe, DayZ_BE.exe, RainbowSix_BE.exe.
BATTLEYE_SUFFIX = "_be.exe"


@dataclass(frozen=True)
class Detection:
    anticheat: str
    process: str

    @property
    def message(self) -> str:
        return (f"Запущен античит {self.anticheat} (процесс {self.process}): съёмка экрана "
                f"отключена. Распознавание экрана — только для одиночных игр без античита; "
                f"закройте игру с античитом и запустите съёмку снова.")


def detect(process_names: Iterable[str]) -> Optional[Detection]:
    for name in process_names:
        lowered = (name or "").lower()
        if lowered in ANTICHEAT_PROCESSES:
            return Detection(ANTICHEAT_PROCESSES[lowered], name)
        if lowered.endswith(BATTLEYE_SUFFIX):
            return Detection("BattlEye", name)
    return None


def process_names() -> List[str]:
    """Имена запущенных процессов — снимок системы (Toolhelp32), без открытия процессов."""
    if not IS_WINDOWS:
        return []
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    names: List[str] = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            names.append(entry.szExeFile)
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return names


def running_anticheat() -> Optional[Detection]:
    return detect(process_names())
