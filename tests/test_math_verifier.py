import unittest

from app.math_verifier import (
    verify_arithmetic_equalities,
    verify_math_answer,
    verify_reference_rational_function,
    verify_symbolic_math,
)


class MathVerifierTests(unittest.TestCase):
    def test_detects_wrong_numeric_equality(self):
        self.assertTrue(verify_arithmetic_equalities("5 + 7 = 13"))
        self.assertEqual(verify_arithmetic_equalities(r"\frac{16}{4}=4"), [])

    def test_ignores_symbolic_equalities(self):
        self.assertEqual(verify_arithmetic_equalities("5x + 7 = 13"), [])

    def test_detects_known_rational_derivative_error(self):
        answer = r"f(x)=\\frac{3x^2+ax+b}{x^2+1}; f'(0)=a+6"
        self.assertTrue(verify_symbolic_math(answer))

    def test_detects_reference_function_wrong_derivative(self):
        answer = r"f(x)=\\frac{x^2+1}{x-1}, f'(x)=\\frac{x^2+1}{(x-1)^2}"
        self.assertTrue(verify_reference_rational_function(answer))

    def test_combines_numeric_and_symbolic_checks(self):
        self.assertGreaterEqual(len(verify_math_answer("5+7=13")), 1)


if __name__ == "__main__":
    unittest.main()
