Name:           yamusic-radio
# CI passes the version of the tag: rpmbuild --define "pkg_version 1.2.3"
Version:        %{?pkg_version}%{!?pkg_version:0.1.0}
Release:        %{?pkg_release}%{!?pkg_release:%autorelease}
Summary:        Endless beat-matched DJ radio from Yandex Music
License:        LGPL-3.0-or-later

BuildArch:      x86_64
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  systemd-rpm-macros

# MP3 output needs ffmpeg with libmp3lame (ffmpeg-free does not have it)
Requires:       /usr/bin/ffmpeg

# The program is one PyInstaller executable: it carries its own Python and
# libraries, and PyInstaller's archive must not be stripped or recompiled.
%global debug_package %{nil}
%global __strip /bin/true
%global __brp_python_bytecompile %{nil}

%description
DJ radio for Yandex Music: takes tracks from a station, beat-matches them
into one endless mix (bass swap, filter sweeps, effects) and streams it as
MP3 over HTTP.

%prep
# Source добавляет rebuild.py, поэтому %%autosetup при разборе спека не работает
tar -xzf %{_sourcedir}/%{name}-%{version}.tar.gz

%build
cd %{name}-%{version}
export SETUPTOOLS_SCM_PRETEND_VERSION=%{version}
%{python3} -m venv %{_builddir}/venv
%{_builddir}/venv/bin/pip install --no-cache-dir pyinstaller
# Essentia finds the keys; it has wheels for x86_64 only. Without it the
# program falls back to a simpler (and worse) key detection.
if %{_builddir}/venv/bin/pip install --no-cache-dir ".[key]"; then
    collect="--collect-all essentia"
else
    %{_builddir}/venv/bin/pip install --no-cache-dir .
    collect=""
fi
printf 'from yamusic_radio.main import main\n\nmain()\n' > %{_builddir}/launcher.py
%{_builddir}/venv/bin/pyinstaller --noconfirm --clean --onefile \
    --name yamusic-dj \
    --distpath %{_builddir}/dist --workpath %{_builddir}/work \
    --specpath %{_builddir} \
    --collect-submodules yandex_music --collect-data yamusic_radio \
    $collect %{_builddir}/launcher.py

%install
install -Dpm0755 %{_builddir}/dist/yamusic-dj %{buildroot}%{_bindir}/yamusic-dj
cd %{name}-%{version}
install -Dpm0644 yamusic_radio/contrib/yamusic-dj.service \
    %{buildroot}%{_userunitdir}/yamusic-dj.service
sed -i 's|%h/.local/bin/yamusic-dj|%{_bindir}/yamusic-dj|' \
    %{buildroot}%{_userunitdir}/yamusic-dj.service

%files
%license %{name}-%{version}/LICENSE %{name}-%{version}/COPYING.LESSER
%{_bindir}/yamusic-dj
%{_userunitdir}/yamusic-dj.service

%changelog
%autochangelog
