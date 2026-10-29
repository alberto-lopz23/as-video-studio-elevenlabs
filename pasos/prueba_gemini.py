"""Pruebas locales sin llamadas de pago."""
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motores.imagen_gemini import imagen

class PruebaGemini(unittest.TestCase):
    def test_modelo_y_formatos(self):
        self.assertEqual(imagen.MODELO, "gemini-3.1-flash-image")
        self.assertEqual(imagen.ASPECTOS["apaisado"], "16:9")
        self.assertEqual(imagen.ASPECTOS["cuadrado"], "1:1")
        self.assertEqual(imagen.ASPECTOS["vertical"], "9:16")

if __name__ == "__main__":
    unittest.main()
