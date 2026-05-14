# Google Maps Downloader - Executable Distribution

This directory contains the standalone executable version of Google Maps Downloader.

## Quick Start

### Windows
1. Extract `map-downloader-windows.zip`
2. Double-click `map-downloader.exe`
3. Open browser and navigate to `http://localhost:5000`

### macOS
1. Extract `map-downloader-macos.tar.gz`
2. Open Terminal in the extracted folder
3. Run: `./map-downloader`
4. Open browser and navigate to `http://localhost:5000`

### Linux
1. Extract `map-downloader-linux.tar.gz`
2. Open Terminal in the extracted folder
3. Run: `./map-downloader`
4. Open browser and navigate to `http://localhost:5000`

## Features

- No Python installation required
- No dependency installation needed
- Portable - can be copied to any machine
- All dependencies bundled inside

## System Requirements

- **Windows**: Windows 10 or later (64-bit)
- **macOS**: macOS 10.15 (Catalina) or later
- **Linux**: Ubuntu 20.04+ or equivalent (64-bit)

## Directory Structure

```
map-downloader/
├── map-downloader(.exe)    # Main executable
├── templates/              # Web UI templates
├── static/                 # CSS, JS, images
├── data/                   # Database (auto-created)
├── downloads/              # Downloaded maps (auto-created)
└── cache/                  # Tile cache (auto-created)
```

## Configuration

The application will automatically create necessary directories on first run:
- `data/` - SQLite database
- `downloads/` - Downloaded map files
- `cache/` - Tile cache for performance

## Troubleshooting

### Port Already in Use
If port 5000 is already in use, the application will fail to start. Close other applications using port 5000 or modify the port in the source code.

### Firewall Warnings
On first run, your firewall may ask for permission. Allow the application to accept incoming connections.

### macOS Security Warning
If macOS blocks the application:
1. Go to System Preferences → Security & Privacy
2. Click "Open Anyway" for map-downloader

### Linux Permissions
If you get a permission error:
```bash
chmod +x map-downloader
```

## Building from Source

If you want to build the executable yourself:

1. Install Python 3.9+
2. Install dependencies: `pip install -r requirements.txt`
3. Install PyInstaller: `pip install pyinstaller`
4. Run: `pyinstaller build.spec`
5. Find executable in `dist/map-downloader/`

## Support

For issues and questions, please visit:
https://github.com/YOUR_USERNAME/map-download/issues
