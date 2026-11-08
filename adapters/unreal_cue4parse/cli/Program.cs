// SPDX-License-Identifier: GPL-3.0-or-later
// otue: opentool's Unreal reader CLI on CUE4Parse. Read-only on the game install; writes only under --out.
//
//   otue ls      --paks <Content/Paks> --game GAME_UE4_27 [--usmap f] --out files.txt
//   otue classes --paks .. --game .. [--usmap f] --prefix <p>[,<p>...] --out classes.json
//                  every export class in packages under the prefixes; SkeletalMesh -> skeleton path, bones, materials;
//                  AnimSequence -> skeleton path, frames, rate
//   otue skeleton --paks .. --game .. --mesh <object path> --out skeleton.json   bones, parents, ref pose, sockets
//   otue export  --paks .. --game .. --format usd|gltf|psk --out <dir> <object path> [<object path> ...]
//
// Encrypted containers are never opened (no key is ever submitted); unversioned packages need --usmap (the user's own).
using System.Text.Json;
using System.Text.Json.Nodes;
using CUE4Parse_Conversion;
using CUE4Parse_Conversion.Options;
using CUE4Parse.FileProvider;
using CUE4Parse.MappingsProvider;
using CUE4Parse.MappingsProvider.Usmap;
using CUE4Parse.UE4.Assets;
using CUE4Parse.UE4.Assets.Exports;
using CUE4Parse.UE4.Assets.Exports.Animation;
using CUE4Parse.UE4.Assets.Exports.SkeletalMesh;
using CUE4Parse.UE4.Objects.UObject;
using CUE4Parse.UE4.Versions;

namespace OtUe;

public static class Program
{
    static Dictionary<string, string> opts = new();
    static List<string> rest = new();

    static string Opt(string k, string? d = null) => opts.TryGetValue(k, out var v) ? v : d ?? throw new ArgumentException($"--{k} is required");

    public static int Main(string[] args)
    {
        for (var i = 1; i < args.Length; i++)
        {
            if (args[i].StartsWith("--") && i + 1 < args.Length) { opts[args[i][2..]] = args[i + 1]; i++; }
            else rest.Add(args[i]);
        }
        try
        {
            var cmd = args.Length > 0 ? args[0] : "help";
            if (cmd == "help") { Console.WriteLine("otue ls|classes|skeleton|export (see Program.cs)"); return 0; }
            var p = Open();
            return cmd switch
            {
                "ls" => Ls(p),
                "classes" => Classes(p),
                "skeleton" => Skeleton(p),
                "export" => Export(p).GetAwaiter().GetResult(),
                "json" => Json(p),
                _ => throw new ArgumentException($"unknown command {cmd}")
            };
        }
        catch (Exception e)
        {
            Console.Error.WriteLine($"otue: {e.GetType().Name}: {e.Message}");
            Console.WriteLine("RESULT_JSON:" + JsonSerializer.Serialize(new { ok = false, error = e.Message }));
            return 2;
        }
    }

    static DefaultFileProvider Open()
    {
        var game = Enum.Parse<EGame>(Opt("game"));
        var p = new DefaultFileProvider(Opt("paks"), SearchOption.TopDirectoryOnly, new VersionContainer(game), StringComparer.OrdinalIgnoreCase);
        var oodle = Opt("oodle", "");  // an Oodle DLL the user already has (the game's own, or theirs); never downloaded
        if (oodle != "") CUE4Parse.Compression.OodleHelper.Initialize(new OodleDotNet.Oodle(oodle));
        var usmap = Opt("usmap", "");
        if (usmap != "") p.MappingsContainer = new FileUsmapTypeMappingsProvider(usmap);
        p.Initialize();
        p.Mount();  // mounts only containers that need no key
        p.PostMount();
        var locked = p.UnloadedVfs.Where(v => v.IsEncrypted).Select(v => v.Name).ToList();
        Console.Error.WriteLine($"otue: mounted {p.MountedVfs.Count} containers, {p.Files.Count} files; encrypted (skipped): {locked.Count}");
        if (locked.Count > 0) Console.Error.WriteLine("otue: encrypted containers are never opened: " + string.Join(", ", locked.Take(8)));
        return p;
    }

    static int Ls(DefaultFileProvider p)
    {
        var files = p.Files.Keys.Where(k => k.EndsWith(".uasset", StringComparison.OrdinalIgnoreCase)).OrderBy(k => k, StringComparer.Ordinal).ToList();
        File.WriteAllLines(Opt("out"), files);
        Console.WriteLine("RESULT_JSON:" + JsonSerializer.Serialize(new { ok = true, packages = files.Count, out_ = Opt("out") }));
        return 0;
    }

    static string? PathOf(FPackageIndex? i) => i is null || i.IsNull ? null : i.ResolvedObject?.GetPathName();

    static int Classes(DefaultFileProvider p)
    {
        var prefixes = Opt("prefix").Split(',', StringSplitOptions.RemoveEmptyEntries);
        var files = p.Files.Values.Where(f => f.Path.EndsWith(".uasset", StringComparison.OrdinalIgnoreCase)
                                              && prefixes.Any(x => f.Path.StartsWith(x, StringComparison.OrdinalIgnoreCase)))
                                  .OrderBy(f => f.Path, StringComparer.Ordinal).ToList();
        var outp = new JsonObject();
        int errors = 0;
        var lk = new object();
        Parallel.ForEach(files, new ParallelOptions { MaxDegreeOfParallelism = Math.Min(8, Environment.ProcessorCount) }, f =>
        {
            var entry = new JsonArray();
            try
            {
                if (!p.TryLoadPackage(f, out var pkg)) throw new Exception("cannot load");
                for (var i = 0; i < pkg.ExportMapLength; i++)
                {
                    var ptr = new FPackageIndex(pkg, i + 1).ResolvedObject;
                    if (ptr is null) continue;
                    var cls = ptr.Class?.Name.Text ?? "?";
                    var e = new JsonObject { ["name"] = ptr.Name.Text, ["class"] = cls, ["path"] = ptr.GetPathName() };
                    if (cls is "SkeletalMesh" or "AnimSequence" or "Skeleton")
                    {
                        try
                        {
                            var o = ptr.Load();
                            switch (o)
                            {
                                case USkeletalMesh m:
                                    e["skeleton"] = PathOf(m.Skeleton);
                                    e["bones"] = m.ReferenceSkeleton.FinalRefBoneInfo.Length;
                                    e["materials"] = new JsonArray(m.Materials.Select(x => (JsonNode?) PathOf(x)).ToArray());
                                    e["lods"] = m.LODModels?.Length ?? 0;
                                    break;
                                case UAnimSequence a:
                                    e["skeleton"] = PathOf(a.Skeleton);
                                    e["frames"] = a.NumFrames;
                                    e["rate"] = a.SequenceLength > 0 ? Math.Round(Math.Max(1, a.NumFrames - 1) / a.SequenceLength, 3) : 30.0;
                                    e["additive"] = a.AdditiveAnimType.ToString();
                                    break;
                                case USkeleton s:
                                    e["bones"] = s.BoneCount;
                                    break;
                            }
                        }
                        catch (Exception ex) { e["error"] = ex.Message; }
                    }
                    entry.Add(e);
                }
            }
            catch (Exception ex) { lock (lk) errors++; entry.Add(new JsonObject { ["error"] = ex.Message }); }
            lock (lk) outp[f.Path] = entry;
        });
        File.WriteAllText(Opt("out"), outp.ToJsonString(new JsonSerializerOptions { WriteIndented = false }));
        Console.WriteLine("RESULT_JSON:" + JsonSerializer.Serialize(new { ok = true, packages = files.Count, errors }));
        return 0;
    }

    static JsonArray V(double x, double y, double z) => new(x, y, z);

    static int Skeleton(DefaultFileProvider p)
    {
        var mesh = p.LoadPackageObject<USkeletalMesh>(Opt("mesh"));
        var rs = mesh.ReferenceSkeleton;
        var bones = new JsonArray();
        for (var i = 0; i < rs.FinalRefBoneInfo.Length; i++)
        {
            var b = rs.FinalRefBoneInfo[i];
            var t = rs.FinalRefBonePose[i];
            bones.Add(new JsonObject
            {
                ["name"] = b.Name.Text, ["parent"] = b.ParentIndex,
                ["t"] = V(t.Translation.X, t.Translation.Y, t.Translation.Z),
                ["q"] = new JsonArray(t.Rotation.X, t.Rotation.Y, t.Rotation.Z, t.Rotation.W),
                ["s"] = V(t.Scale3D.X, t.Scale3D.Y, t.Scale3D.Z)
            });
        }
        var sockets = new JsonArray();
        void AddSockets(IEnumerable<FPackageIndex> idx, string owner)
        {
            foreach (var si in idx)
            {
                if (!si.TryLoad<USkeletalMeshSocket>(out var s) || s is null) continue;
                sockets.Add(new JsonObject
                {
                    ["name"] = s.SocketName.Text, ["bone"] = s.BoneName.Text, ["owner"] = owner,
                    ["loc"] = V(s.RelativeLocation.X, s.RelativeLocation.Y, s.RelativeLocation.Z),
                    ["rot_pyr"] = V(s.RelativeRotation.Pitch, s.RelativeRotation.Yaw, s.RelativeRotation.Roll),
                    ["scale"] = V(s.RelativeScale.X, s.RelativeScale.Y, s.RelativeScale.Z)
                });
            }
        }
        AddSockets(mesh.Sockets, "mesh");
        string? skelPath = PathOf(mesh.Skeleton);
        if (mesh.Skeleton is not null && !mesh.Skeleton.IsNull && mesh.Skeleton.TryLoad<USkeleton>(out var sk) && sk is not null)
            AddSockets(sk.Sockets, "skeleton");
        var o = new JsonObject { ["mesh"] = Opt("mesh"), ["skeleton"] = skelPath, ["units"] = "cm, Z-up, left-handed (Unreal)", ["bones"] = bones, ["sockets"] = sockets };
        File.WriteAllText(Opt("out"), o.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
        Console.WriteLine("RESULT_JSON:" + JsonSerializer.Serialize(new { ok = true, bones = bones.Count, sockets = sockets.Count }));
        return 0;
    }

    // otue json --paks .. --game .. --out <file.json> <package path>   every export's properties (blueprint defaults, data assets)
    static int Json(DefaultFileProvider p)
    {
        var pkg = p.LoadPackage(rest[0]);
        File.WriteAllText(Opt("out"), Newtonsoft.Json.JsonConvert.SerializeObject(pkg.GetExports(), Newtonsoft.Json.Formatting.Indented));
        Console.WriteLine("RESULT_JSON:" + JsonSerializer.Serialize(new { ok = true, exports = pkg.ExportMapLength }));
        return 0;
    }

    static async Task<int> Export(DefaultFileProvider p)
    {
        var fmt = Opt("format", "usd") switch { "usd" => EMeshFormat.USD, "gltf" => EMeshFormat.Gltf2, "psk" => EMeshFormat.ActorX, var f => throw new ArgumentException($"format {f}") };
        var session = new ExportSession { MaxDegreeOfParallelism = Math.Min(4, Environment.ProcessorCount) };
        foreach (var path in rest)
            session.Add(p.LoadPackageObject(path));
        var res = await session.RunAsync(Opt("out"), new ExportOptions(meshFormat: fmt, socketFormat: ESocketFormat.Bone, exportMorphTargets: false,
                                                                     textureFormat: ETextureFormat.Png));
        var files = new JsonArray();
        foreach (var r in res) files.Add(JsonSerializer.SerializeToNode(r.ToString()));
        Console.WriteLine("RESULT_JSON:" + new JsonObject { ["ok"] = true, ["results"] = files }.ToJsonString());
        return 0;
    }
}
