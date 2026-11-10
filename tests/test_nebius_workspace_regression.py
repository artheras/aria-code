import unittest

from aria_code.model_capability import get_model_capability, is_unknown_model
from aria_code.apps.cli.workspace_route import route_has_tools
from aria_code.apps.cli.utils.market_detect import _is_broker_setup_intent


class NebiusWorkspaceRegressionTests(unittest.TestCase):
    def test_verified_nemotron_model_can_enter_workspace_tool_loop(self):
        model = 'nvidia/nemotron-3-super-120b-a12b'
        cap = get_model_capability(model)
        self.assertTrue(cap.tool_calls)
        self.assertEqual(cap.format, 'openai_native')
        self.assertFalse(is_unknown_model(cap))
        self.assertTrue(route_has_tools(model, {'local_provider': 'custom', 'backend_chat': False}, None))

    def test_unknown_nvidia_model_remains_conservative(self):
        self.assertFalse(get_model_capability('nvidia/unverified-model').tool_calls)

    def test_inventory_quantities_do_not_open_broker_wizard(self):
        self.assertFalse(_is_broker_setup_intent('Add inventory tests rejecting negative quantities and fractional quantities.'))

    def test_actual_broker_setup_still_routes(self):
        self.assertTrue(_is_broker_setup_intent('add broker'))
        self.assertTrue(_is_broker_setup_intent('configure quant'))


if __name__ == '__main__':
    unittest.main()
