import unittest
from aria_code.packs import load_builtin_packs, activate_packs, reset_builtin_packs
from aria_code.packs.healthcare import HealthcarePack


class HealthcarePackTests(unittest.TestCase):
    def test_explicit_identifiers_only(self):
        pack = HealthcarePack()
        for text in ("分析医疗市场", "我头疼怎么办", "healthcare API", "12345678", "PMID: 1234567890", "xPMID: 12345"):
            with self.subTest(text=text):
                self.assertFalse(pack.resolve_entities(text))
        found = pack.resolve_entities("比较PMID: 12345 和 https://pubmed.ncbi.nlm.nih.gov/54321/ 以及 PMC7654321 和 NCT01234567")
        self.assertEqual([m.value for m in found], ["12345", "54321", "PMC7654321", "NCT01234567"])

    def test_registered_and_no_clinical_validation_claim(self):
        reset_builtin_packs()
        self.assertIn("healthcare", load_builtin_packs())
        active = activate_packs("Summarize PMID: 12345")
        self.assertEqual([a.pack for a in active], ["healthcare"])
        self.assertIn("pending human review", HealthcarePack().prompt_fragment(active[0]))


if __name__ == '__main__':
    unittest.main()
