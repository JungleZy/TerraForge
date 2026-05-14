# Building Executables

## Overview

This project can be packaged into standalone executables for Windows, macOS, and Linux using PyInstaller. The executables include all dependencies and can run on machines without Python installed.

## Automated Build (GitHub Actions)

### Triggering a Build

The GitHub Actions workflow automatically builds executables for all platforms when you:

1. **Push a tag** (recommended for releases):
   ```bash
   git tag -a v1.0.0 -m "Release version 1.0.0"
   git push origin v1.0.0
   ```

2. **Manual trigger** via GitHub web interface:
   - Go to Actions tab
   - Select "Build Executables" workflow
   - Click "Run workflow"

### Download Built Executables

After the workflow completes:
- Go to the Actions tab
- Click on the completed workflow run
- Download artifacts from the "Artifacts" section
- For tagged releases, executables are also attached to the GitHub Release

## Local Build

### Prerequisites

1. **Python 3.9+** installed
2. **GDAL** installed:
   - **Ubuntu/Debian**: `sudo apt-get install gdal-bin libgdal-dev`
   - **macOS**: `brew install gdal`
   - **Windows**: `choco install gdal` or download from https://gdal.org

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   pip install pyinstaller
   ```

### Build Commands

#### Linux/macOS
```bash
./build.sh
```

#### Windows
```cmd
build.bat
```

#### Manual Build
```bash
pyinstaller build.spec
```

### Output

The executable will be created in:
```
dist/map-downloader/
├── map-downloader(.exe)    # Main executable
├── templates/              # Web UI templates
├── static/                 # Static assets
└── [other bundled files]
```

## Distribution

### Package for Distribution

#### Linux/macOS
```bash
cd dist
tar -czf map-downloader-linux.tar.gz map-downloader/
```

#### Windows
```cmd
cd dist
powershell Compress-Archive -Path map-downloader/* -DestinationPath map-downloader-windows.zip
```

### Testing the Executable

1. Navigate to the dist folder:
   ```bash
   cd dist/map-downloader
   ```

2. Run the executable:
   - **Linux/macOS**: `./map-downloader`
   - **Windows**: `map-downloader.exe`

3. Open browser to `http://localhost:5000`

## Troubleshooting

### GDAL Issues

If GDAL fails to load in the executable:

1. **Check GDAL installation**:
   ```bash
   gdal-config --version  # Linux/macOS
   gdalinfo --version     # Windows
   ```

2. **Verify GDAL_DATA path** in `hook-gdal.py`

3. **Rebuild with verbose output**:
   ```bash
   pyinstaller --log-level DEBUG build.spec
   ```

### Missing Dependencies

If the executable fails with import errors:

1. Add missing modules to `hiddenimports` in `build.spec`
2. Rebuild the executable

### Large Executable Size

To reduce size:

1. Remove unused dependencies from `requirements.txt`
2. Use `--exclude-module` in `build.spec` for unnecessary packages
3. Disable UPX compression if it causes issues: set `upx=False` in `build.spec`

### Platform-Specific Issues

#### macOS: "App is damaged"
```bash
xattr -cr dist/map-downloader
```

#### Linux: Permission denied
```bash
chmod +x dist/map-downloader/map-downloader
```

#### Windows: Antivirus false positive
- Add exception in antivirus software
- Sign the executable with a code signing certificate

## Build Configuration

### build.spec

The `build.spec` file controls the build process:

- **datas**: Include templates, static files, GDAL data
- **hiddenimports**: Python modules not auto-detected
- **binaries**: Native libraries (GDAL, etc.)
- **runtime_hooks**: Setup code that runs before app starts

### Customization

To customize the build:

1. **Change app name**: Modify `name='map-downloader'` in `build.spec`
2. **Add icon**: Add `icon='icon.ico'` to `EXE()` section
3. **Single file mode**: Replace `COLLECT()` with single-file EXE
4. **Console mode**: Set `console=False` to hide terminal window

## CI/CD Integration

The `.github/workflows/build.yml` workflow:

1. Sets up Python 3.9
2. Installs GDAL for each platform
3. Installs Python dependencies
4. Builds executable with PyInstaller
5. Packages the result
6. Uploads artifacts
7. Creates GitHub Release (for tags)

### Workflow Customization

Edit `.github/workflows/build.yml` to:
- Change Python version
- Add code signing
- Modify packaging format
- Add additional build steps

## Security Considerations

- Executables include all source code (can be extracted)
- Consider obfuscation for sensitive code
- Sign executables for production distribution
- Scan with antivirus before distribution

## Performance

Executable startup is slower than Python script due to:
- Unpacking bundled files
- Loading all dependencies

To improve:
- Use `--onefile` for faster startup (but larger file)
- Exclude unused modules
- Use lazy imports in code

## Support

For build issues:
1. Check PyInstaller documentation: https://pyinstaller.org
2. Review build logs in GitHub Actions
3. Open an issue with build output
