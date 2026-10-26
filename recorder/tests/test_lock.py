"""Un solo grabador por directorio de datos.

El caso que motiva esto es real: dos procesos escribiendo el mismo HH.jsonl
entrelazan líneas y producen JSON corrupto que no se detecta hasta meses
después, cuando ya no sabes cuánto del entorno es basura.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from liqrec.lock import AlreadyRunning, InstanceLock


class TestInstanceLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        # test_lock_survives_a_dropped_reference deja un cerrojo abierto a
        # propósito (esa es la prueba). En Windows no se puede borrar un
        # fichero con el descriptor todavía abierto y bloqueado, así que hay
        # que soltarlo antes de limpiar el directorio temporal. En Linux esto
        # es un no-op: unlink sobre un fd abierto no falla.
        from liqrec.lock import _HELD
        for lk in list(_HELD):
            if lk.path.is_relative_to(self.root):
                lk.release()
        self.tmp.cleanup()

    def test_second_acquire_is_refused(self):
        first = InstanceLock(self.root).acquire()
        try:
            with self.assertRaises(AlreadyRunning) as cm:
                InstanceLock(self.root).acquire()
            self.assertIn("ya hay un grabador", str(cm.exception))
        finally:
            first.release()

    def test_error_names_who_holds_it(self):
        first = InstanceLock(self.root).acquire()
        try:
            with self.assertRaises(AlreadyRunning) as cm:
                InstanceLock(self.root).acquire()
            self.assertIn(str(os.getpid()), cm.exception.holder)
        finally:
            first.release()

    def test_release_lets_the_next_one_in(self):
        InstanceLock(self.root).acquire().release()
        second = InstanceLock(self.root).acquire()   # no debe lanzar
        second.release()

    def test_context_manager(self):
        with InstanceLock(self.root):
            with self.assertRaises(AlreadyRunning):
                InstanceLock(self.root).acquire()
        InstanceLock(self.root).acquire().release()   # ya libre

    def test_different_data_dirs_do_not_collide(self):
        a = InstanceLock(self.root / "a").acquire()
        b = InstanceLock(self.root / "b").acquire()
        a.release()
        b.release()

    def test_lock_is_released_when_the_process_dies_hard(self):
        """El motivo de usar un cerrojo del sistema y no un PID en un fichero:
        un SIGKILL no deja huérfano el cerrojo."""
        script = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
            from liqrec.lock import InstanceLock
            # Sin asignar a propósito: comprueba que el cerrojo sobrevive a que
            # nadie guarde la referencia (lo garantiza el registro _HELD).
            InstanceLock({str(self.root)!r}).acquire()
            print("locked", flush=True)
            time.sleep(60)
        """)
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            with self.assertRaises(AlreadyRunning):
                InstanceLock(self.root).acquire()
            proc.kill()
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
        # El sistema operativo ya soltó el cerrojo al morir el proceso.
        InstanceLock(self.root).acquire().release()

    def test_lock_survives_a_dropped_reference(self):
        """Sin el registro interno, el recolector cerraría el fichero y el
        sistema soltaría el cerrojo sin avisar."""
        import gc
        InstanceLock(self.root).acquire()      # nadie guarda el resultado
        gc.collect()
        with self.assertRaises(AlreadyRunning):
            InstanceLock(self.root).acquire()

    def test_holder_info_lives_in_a_separate_file(self):
        """El texto va aparte del fichero bloqueado.

        Escribir en el propio fichero del cerrojo obliga a truncarlo, y en
        Windows truncar por debajo de una región bloqueada puede fallar con
        violación de cerrojo. Separados, sobre el fichero del cerrojo no se
        escribe nunca.
        """
        lk = InstanceLock(self.root).acquire()
        try:
            info = (self.root / "recorder.lock.info").read_text(encoding="utf-8")
            self.assertIn(f"pid {os.getpid()}", info)
            self.assertEqual((self.root / "recorder.lock").stat().st_size, 0,
                             "sobre el fichero del cerrojo no se escribe jamás")
        finally:
            lk.release()

    def test_info_is_cleaned_up_on_release(self):
        InstanceLock(self.root).acquire().release()
        self.assertFalse((self.root / "recorder.lock.info").exists())


if __name__ == "__main__":
    unittest.main()
