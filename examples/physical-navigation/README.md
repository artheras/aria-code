# Navigation fixture

The native Rust simulator and its interactive UI are documented in
[Native navigation simulation](../../docs/physical-navigation.md).

Build `aria-physical` or use `aria-native physical`. No Python worker, cloud key,
robot, GPU or paid API is required to run the grid service. Python is used only
by its independent acceptance tests.

The fixture is a grid task, not a physical warehouse digital twin. It is intended
for observation/action protocol development and reproducible navigation checks.
