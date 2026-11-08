# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the Unreal reader (otue.exe) into a folder of yours, from this repo's source and pinned packages.

    python -I adapters/unreal_cue4parse/build_reader.py <reader folder> [--natives <CUE4Parse-Natives source folder>]

Needs the .NET 10 SDK. `dotnet restore` downloads the 42 NuGet packages pinned in cli/packages.lock.json from
nuget.org into <reader folder>/nuget (CUE4Parse + CUE4Parse-Conversion 1.2.2.202610 and their dependencies).
Then [readers] ue_cli = <reader folder>/bin/otue.exe.

ACL-compressed animations (most UE4.25+ games) also need CUE4Parse-Natives.dll next to otue.exe. NuGet does not
ship it; build it from source (ACL part only, no Oodle) with --natives pointing at a CUE4Parse-Natives folder that
holds ACL/external/acl (nfrechette/acl at the commit CUE4Parse pins) with acl/external/rtm. Sources the tool was
tested with (download them yourself): CUE4Parse tag 1.2.2.202610, acl 414689d5cff4286a7898487a46dc5e48005d38da,
rtm d7982f2b2524feeb322f424b29cf43df30b5d5b7. Needs CMake and a C++ compiler (Visual Studio Build Tools).
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def cmake_exe():
    c = shutil.which("cmake")
    if c:
        return c
    hits = glob.glob(r"C:\Program Files*\Microsoft Visual Studio\*\*\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe")
    return hits[0] if hits else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dest")
    ap.add_argument("--natives", help="CUE4Parse-Natives source folder (with ACL/external/acl)")
    a = ap.parse_args()
    dest = os.path.abspath(a.dest)
    src = os.path.join(dest, "src")
    os.makedirs(src, exist_ok=True)
    for f in ("OtUe.csproj", "Program.cs", "packages.lock.json"):
        shutil.copy2(os.path.join(HERE, "cli", f), src)
    env = dict(os.environ, NUGET_PACKAGES=os.path.join(dest, "nuget"), DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1")
    if not shutil.which("dotnet"):
        raise SystemExit("dotnet not found: install the .NET 10 SDK")
    subprocess.run(["dotnet", "restore", "--locked-mode"], cwd=src, env=env, check=True)
    subprocess.run(["dotnet", "build", "-c", "Release", "--no-restore", "-o", os.path.join(dest, "bin")], cwd=src, env=env, check=True)
    if a.natives:
        cm = cmake_exe()
        if not cm:
            raise SystemExit("cmake not found (install CMake or Visual Studio Build Tools with C++)")
        if not os.path.exists(os.path.join(a.natives, "ACL", "external", "acl", "includes")):
            raise SystemExit(f"{a.natives}: no ACL/external/acl/includes (unpack nfrechette/acl there, rtm in acl/external/rtm)")
        b = os.path.join(dest, "natives-build")
        subprocess.run([cm, "-S", a.natives, "-B", b, "-A", "x64"], check=True)
        subprocess.run([cm, "--build", b, "--config", "Release"], check=True)
        dll = glob.glob(os.path.join(b, "**", "CUE4Parse-Natives.dll"), recursive=True)
        if not dll:
            raise SystemExit("natives build produced no CUE4Parse-Natives.dll")
        shutil.copy2(dll[0], os.path.join(dest, "bin"))
    exe = os.path.join(dest, "bin", "otue.exe")
    print(f"built {exe}" + ("" if a.natives else "  (no CUE4Parse-Natives.dll: ACL-compressed clips will fail)"))
    print(f'opentool.toml:  [readers] ue_cli = "{exe.replace(os.sep, "/")}"')


if __name__ == "__main__":
    sys.exit(main())
