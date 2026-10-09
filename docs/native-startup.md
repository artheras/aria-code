# Native installation and first startup

`aria update` and the macOS/Linux installer verify the release checksum and
version before replacing the installed command. They also run the full CLI's
`--help` at its final installation path, from the managed build directory.
This loads the bundled CLI modules without starting a session or contacting
a model, and does not use the user's current project as its working directory.
The installer displays `Preparing the CLI for its first startup...` during
this check. A failed check keeps the previous command and removes the new build;
the updater also limits this check to 120 seconds.

The version check alone exits before the full CLI imports. It can pass even
when a required module is missing or the next interactive launch still needs
macOS to validate additional native libraries. Preparing those imports during
installation moves that first-load wait into the visible installation step.
It does not disable certificate verification, Gatekeeper, or library checks.

This addresses installation through the native updater and the macOS/Linux
installer. npm's launcher still reports its first-launch notice on macOS;
manually unpacked release archives may still have a slow first launch. Releases
without Developer ID signing and Apple notarization are not claimed to be
Gatekeeper-ready. Configure the existing signing/notarization workflow secrets
to distribute signed macOS builds.

Normal CLI startup restores saved tasks in one read. It does not rewrite every
finished task; interrupted running tasks are persisted in one batch. A large
task history therefore no longer adds a full-file rewrite per saved task.
PDF, Word, Excel and dataframe parsers are loaded when `/file` is first used,
rather than constructing an unused file-analysis session before the prompt.
Registering the spreadsheet tool checks availability without loading openpyxl;
that runtime is loaded when a workbook is first generated.
