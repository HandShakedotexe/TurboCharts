"""Un solo grabador por directorio de datos.

Dos procesos escribiendo el mismo `HH.jsonl` a la vez entrelazan líneas y
producen JSON corrupto a mitad de fichero. Es un fallo silencioso y tardío: no
te enteras hasta que meses después el parser revienta en una línea rara y ya no
sabes cuánto de lo que hay alrededor es basura.

Se usa un cerrojo de sistema operativo, no un PID guardado en disco. El sistema
suelta el cerrojo cuando el proceso muere, sea como sea que muera —incluido un
kill -9, un pantallazo azul o un corte de luz— mientras que un PID guardado a
mano se queda huérfano y hay que inventar heurísticas para adivinar si vive.

DOS DECISIONES QUE PARECEN RAREZAS Y NO LO SON:

1. El cerrojo se toma sobre un descriptor CRUDO (os.open), no sobre un objeto
   file de Python. En Windows, msvcrt.locking bloquea a partir de la posición
   actual del descriptor, y mezclar eso con el búfer de un objeto file lleva a
   bloquear una región distinta de la que crees.

2. El texto de "quién lo tiene" va en un fichero APARTE. Escribir en el mismo
   fichero que está bloqueado obliga a truncarlo, y en Windows truncar por
   debajo de una región bloqueada puede fallar con violación de cerrojo. Al
   separarlos, sobre el fichero del cerrojo no se escribe jamás.
"""
from __future__ import annotations

import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

LOCK_BYTE_OFFSET = 1 << 20   # región bloqueada, más allá de cualquier contenido

# Los cerrojos vivos se guardan aquí a propósito. El cerrojo real es el
# descriptor abierto: si el objeto InstanceLock se queda sin referencias, el
# recolector cierra el descriptor y el sistema suelta el cerrojo sin que nadie
# se entere. Escribir `InstanceLock(d).acquire()` sin asignar el resultado
# bastaría para desactivar la protección en silencio.
_HELD: set = set()


class AlreadyRunning(RuntimeError):
    def __init__(self, path: Path, holder: str):
        self.path = path
        self.holder = holder
        super().__init__(
            f"ya hay un grabador usando este directorio de datos.\n"
            f"  cerrojo : {path}\n"
            f"  lo tiene: {holder}\n\n"
            f"Dos grabadores sobre el mismo directorio corrompen los ficheros.\n"
            f"Para el que ya está corriendo antes de arrancar otro, o usa\n"
            f"--data-dir distinto si de verdad quieres dos en paralelo."
        )


def _try_lock(fd: int) -> None:
    """Toma el cerrojo exclusivo o lanza OSError si ya está tomado."""
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, LOCK_BYTE_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class InstanceLock:
    def __init__(self, data_dir: Path, name: str = "recorder.lock"):
        self.path = Path(data_dir) / name
        self.info_path = self.path.with_suffix(".lock.info")
        self._fd = None

    def _describe_holder(self) -> str:
        try:
            text = self.info_path.read_text(encoding="utf-8", errors="replace").strip()
            return text.splitlines()[0] if text else "otro proceso"
        except OSError:
            return "otro proceso (no dejó constancia de quién es)"

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _try_lock(fd)
        except OSError:
            holder = self._describe_holder()
            os.close(fd)
            raise AlreadyRunning(self.path, holder) from None

        self._fd = fd
        _HELD.add(self)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        try:
            self.info_path.write_text(
                f"pid {os.getpid()} en {socket.gethostname()} desde {stamp}\n",
                encoding="utf-8",
            )
        except OSError:
            pass   # informativo; el cerrojo real ya está tomado
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        _HELD.discard(self)
        _unlock(self._fd)
        try:
            os.close(self._fd)
        finally:
            self._fd = None
        try:
            self.info_path.unlink()
        except OSError:
            pass
        # El fichero del cerrojo se deja en disco a propósito: borrarlo abre
        # una carrera con otro proceso que podría estar bloqueando ya el inode.

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
