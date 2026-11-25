# Third-party notices

Akin Video Finder itself is released under the MIT License; see `LICENSE`.

The Windows build bundles the runtime packages below. The full license texts
are in `licenses/` next to this file and are installed with the application.

## Bundled runtime

| Package | Version | License | License text |
|---|---:|---|---|
| Python | 3.12.10 | Python-2.0 (PSF-2.0) | `licenses/Python-2.0.txt` |
| PySide6, PySide6_Essentials, PySide6_Addons | 6.11.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | `licenses/LGPL-3.0.txt`, `licenses/GPL-3.0.txt` |
| Shiboken6 | 6.11.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | `licenses/LGPL-3.0.txt`, `licenses/GPL-3.0.txt` |
| Qt libraries | 6.11.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | `licenses/LGPL-3.0.txt`, `licenses/GPL-3.0.txt` |
| NumPy | 2.5.4 | BSD-3-Clause and bundled component licenses | `licenses/NumPy-BSD-3-Clause.txt` |
| Typer | 0.27.2 | MIT | `licenses/Typer-MIT.txt` |
| Click (vendored in Typer) | bundled | BSD-3-Clause | `licenses/Click-BSD-3-Clause.txt` |
| Rich | 15.0.0 | MIT | `licenses/Rich-MIT.txt` |
| Markdown-it-py | 4.2.0 | MIT | `licenses/Markdown-it-py-MIT.txt` |
| Markdown-it (vendored) | bundled | MIT | `licenses/Markdown-it-MIT.txt` |
| Pygments | 2.21.0 | BSD-2-Clause | `licenses/Pygments-BSD-2-Clause.txt` |
| mdurl | 0.1.2 | MIT | `licenses/mdurl-MIT.txt` |
| Shellingham | 1.5.4 | ISC | `licenses/Shellingham-ISC.txt` |
| Colorama | 0.4.6 | BSD-3-Clause | `licenses/Colorama-BSD-3-Clause.txt` |
| annotated-doc | 0.0.5 | MIT | `licenses/Annotated-doc-MIT.txt` |
| typing_extensions | 4.16.0 | PSF-2.0 | `licenses/typing_extensions-PSF-2.0.txt` |
| PyInstaller bootloader | 6.22.3 | GPL-2.0-or-later with the PyInstaller exception | `licenses/PyInstaller-GPL-2.0-with-exception.txt` |

The PyInstaller exception allows the bootloader to be used in a program that
is not itself GPL-licensed. The Akin Video Finder source remains MIT.

## Qt and PySide6

The Qt and PySide6 libraries are used unmodified under the LGPL-3.0 option.
The Qt source, including its third-party license files, is available from:

- https://code.qt.io/cgit/qt/qtbase.git/tree/LICENSES
- https://code.qt.io/cgit/qt/
- https://code.qt.io/cgit/pyside/pyside-setup.git/

The commercial licensing reference supplied with the PySide6 wheel is in
`licenses/Qt-Commercial-Reference.txt`. Akin Video Finder is not commercially
licensed by The Qt Company.

The installer bundles the unmodified PySide6/Qt binaries in the GUI
executable. The Akin Video Finder source (MIT), the PySide6/Qt source links above,
and a Python 3.12 environment are sufficient to rebuild the application
against a modified Qt build.

## FFmpeg

FFmpeg is not bundled. The application expects FFmpeg and FFprobe on `PATH`
or configured in `config.toml`. FFmpeg is a separate program under its own
license; installing it does not change the license of Akin Video Finder.

## Development-only packages

`pytest`, `mypy`, `ruff`, `keep-the-why-lint`, PyInstaller's build-time
helpers, and the packaging tools are used to develop and build Akin Video Finder.
They are not part of the installed application.
