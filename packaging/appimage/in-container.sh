#!/bin/sh
# Runs inside the bookworm build container (build-appimage.sh starts it):
#   /kit    packaging/ (read-only)      /stage  the staged app (read-only)
#   /tools  appimagetool and runtime    /cache  pip's cache
#   /out    where the AppImage goes
set -eu
app() { python3 -c 'import json,sys; print(json.load(open("/stage/app.json"))[sys.argv[1]])' "$1"; }
exe=$(app executable); id=$(app id); name=$(app name); file_name=$(app file_name)

echo "== venv and build requirements"
python3 -m venv /work/venv
/work/venv/bin/pip install --quiet --disable-pip-version-check --cache-dir /cache/pip \
    -r /kit/appimage/build-requirements.txt -r /stage/requirements.txt

echo "== PyInstaller"
# The app's own spec when it staged one (shackpkg.py: [pyinstaller] spec,
# for Qt apps), else the kit's.
spec=/kit/pyinstaller/shack-app.spec
[ -f /stage/app.spec ] && spec=/stage/app.spec
SHACK_STAGE=/stage /work/venv/bin/pyinstaller --noconfirm --clean --log-level WARN \
    --distpath /work/dist --workpath /work/pyi "$spec"
bundle=/work/dist/$exe

echo "== host libraries out (host-libs.txt)"
# The app's own list when it staged one (a Qt app keeps the xcb-util
# libraries Qt's xcb plugin needs; the kit's list is for WebKitGTK apps).
hostlibs=/kit/appimage/host-libs.txt
[ -f /stage/host-libs.txt ] && hostlibs=/stage/host-libs.txt
grep -v '^#' "$hostlibs" | grep -v '^$' | while read -r pat; do
    find "$bundle/_internal" -maxdepth 1 -name "$pat" -print -exec rm -f {} + | sed 's|.*/|   - |'
done
echo "== AppDir"
appdir=/work/AppDir
mkdir -p "$appdir/usr/lib" "$appdir/usr/share/applications" "$appdir/usr/share/metainfo"
cp -a "$bundle" "$appdir/usr/lib/$exe"
sed "s/@EXE@/$exe/g" /kit/appimage/AppRun > "$appdir/AppRun"
chmod 755 "$appdir/AppRun"
for s in 16 32 48 64 128 256 512; do
    install -D -m 644 "/stage/icons/$s.png" "$appdir/usr/share/icons/hicolor/${s}x${s}/apps/$id.png"
done
install -m 644 /stage/icons/256.png "$appdir/$id.png"
ln -s "$id.png" "$appdir/.DirIcon"
cats=$(python3 -c 'import json; print(";".join(json.load(open("/stage/app.json"))["categories"]) + ";")')
summary=$(app summary)
cat > "$appdir/$id.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=$name
Comment=$summary
Exec=$exe
Icon=$id
Terminal=false
Categories=$cats
X-AppImage-Version=$(app version)
DESK
desktop-file-validate "$appdir/$id.desktop"
cp "$appdir/$id.desktop" "$appdir/usr/share/applications/"

echo "== appimagetool"
out="/out/$file_name-linux-x86_64.AppImage"
# Extract-and-run: no FUSE inside a container. The runtime is passed in
# (pinned, checksummed by build-appimage.sh) so appimagetool never
# downloads one of its own choosing.
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 /tools/appimagetool --no-appstream \
    --runtime-file /tools/runtime-x86_64 "$appdir" "$out" >/work/appimagetool.log 2>&1 || {
    cat /work/appimagetool.log; exit 1; }
chmod 755 "$out"
echo "built $out"
