%global appdir /opt/yamusic-dj

Name:           yamusic-radio
Version:        0.1.0
Release:        %autorelease
Summary:        Endless beat-matched DJ radio from Yandex Music
License:        LGPL-3.0-or-later

BuildArch:      x86_64
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools
BuildRequires:  python3-setuptools_scm
BuildRequires:  systemd-rpm-macros

Requires:       python3 >= 3.12
Requires:       python3-aiohttp
Requires:       python3-numpy
# MP3 output needs ffmpeg with libmp3lame (ffmpeg-free does not have it)
Requires:       /usr/bin/ffmpeg

# yandex-music отсутствует в Fedora — ставится в venv из PyPI
%global __brp_python_bytecompile %{nil}
%global __requires_exclude_from ^%{appdir}/.*$
%global __provides_exclude_from ^%{appdir}/.*$

%description
DJ radio for Yandex Music: takes tracks from a station, beat-matches them
into one endless mix (bass swap, filter sweeps, effects) and streams it as
MP3 over HTTP.

%prep
# Source добавляет rebuild.py, поэтому %%autosetup при разборе спека не работает
tar -xzf %{_sourcedir}/%{name}-%{version}.tar.gz

%build
# venv создаётся в %%install: buildroot чистится перед ним

%install
cd %{name}-%{version}
export SETUPTOOLS_SCM_PRETEND_VERSION=%{version}
%{python3} -m venv --system-site-packages %{buildroot}%{appdir}
%{buildroot}%{appdir}/bin/pip install --no-cache-dir "yandex-music>=3.2.0"
%{buildroot}%{appdir}/bin/pip install --no-cache-dir --no-deps .
sed -i "s|%{buildroot}||g" %{buildroot}%{appdir}/bin/* %{buildroot}%{appdir}/pyvenv.cfg 2>/dev/null || true
install -d %{buildroot}%{_bindir}
ln -s %{appdir}/bin/yamusic-dj %{buildroot}%{_bindir}/yamusic-dj
install -Dpm0644 yamusic_radio/contrib/yamusic-dj.service \
    %{buildroot}%{_userunitdir}/yamusic-dj.service
sed -i 's|%h/.local/bin/yamusic-dj|%{_bindir}/yamusic-dj|' \
    %{buildroot}%{_userunitdir}/yamusic-dj.service

%files
%license %{name}-%{version}/LICENSE %{name}-%{version}/COPYING.LESSER
%{appdir}
%{_bindir}/yamusic-dj
%{_userunitdir}/yamusic-dj.service

%changelog
%autochangelog
