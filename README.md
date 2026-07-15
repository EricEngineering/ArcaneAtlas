<img src="arcaneatlas/resources/icon.png" alt="Arcane Atlas logo" width="150">

# Arcane Atlas

**A digital battle map for in-person TTRPGs** — part of the [Arcane Tools](https://arcanetools.org) suite.

A battle map, image or video, on your LCD table or a TV, with fog of war you
control. Prefer minis? Use them. Prefer tokens? Players can move their own from a
phone or tablet by scanning a QR code. The player-view window shows you, the GM,
exactly what your players will see before you reveal it. You just have to enter the
LCD dimensions and the grid will size perfectly.

**[⬇ Download for Windows, macOS & Linux →](https://arcanetools.org)**

## Support

The software is free, forever — open-source (AGPLv3), no paywall, no account, no
upsell. A few things still cost money, though: code-signing certificates run about
$250 a year (Apple's Developer Program to notarize the macOS builds, and Microsoft
Trusted Signing for Windows), and domain names and hosting are another $50. If these
tools are useful to you, your support on [Patreon](https://www.patreon.com/c/EricEngineering/membership)
helps cover the cost and keeps development going. Thank you!

## Building

Rebuild the UI:

```
pyside6-uic mainwindow.ui -o ui_mainwindow.py
```

Windowed build with PyInstaller:

```
pyinstaller arcaneatlas.spec
```

## License

Arcane Atlas is licensed under the GNU Affero General Public License v3.0 (AGPLv3). See [license.txt](license.txt).
