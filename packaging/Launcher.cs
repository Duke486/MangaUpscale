using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Threading;
using System.Windows.Forms;

class Launcher
{
    const string Version = "5.0.0";
    const string PayloadHash = "PAYLOAD_HASH";
    const string PayloadParts = "PAYLOAD_PARTS";

    [STAThread]
    static void Main(string[] args)
    {
        Application.EnableVisualStyles();
        try
        {
            string parent = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "MangaUpscale");
            string install = Path.Combine(parent, Version);
            using (Mutex mutex = new Mutex(false, "Local\\MangaUpscaleInstall500"))
            {
                bool acquired;
                try { acquired = mutex.WaitOne(0); }
                catch (AbandonedMutexException) { acquired = true; }
                if (!acquired)
                {
                    MessageBox.Show("另一个窗口正在准备运行环境，请等待它完成。", "漫画超分");
                    return;
                }
                try
                {
                    if (!File.Exists(Path.Combine(install, "installed.ok")))
                    {
                        if (Directory.Exists(install))
                            throw new IOException("运行目录不完整，请将此目录移走后重试：" + install);
                        using (Form form = new Form())
                        {
                            form.Text = "漫画超分 · 首次启动";
                            form.ClientSize = new Size(460, 130);
                            form.FormBorderStyle = FormBorderStyle.FixedDialog;
                            form.StartPosition = FormStartPosition.CenterScreen;
                            form.MaximizeBox = false;
                            form.ControlBox = false;
                            Label label = new Label { Text = "正在解包运行环境，完成后自动打开。\n首次启动约需几分钟，后续无需解包。", AutoSize = false, Location = new Point(24, 20), Size = new Size(410, 50) };
                            ProgressBar bar = new ProgressBar { Style = ProgressBarStyle.Marquee, Location = new Point(24, 85), Size = new Size(410, 15) };
                            form.Controls.Add(label);
                            form.Controls.Add(bar);
                            form.Show();
                            Application.DoEvents();
                            Install(parent, install, label);
                            form.Close();
                        }
                    }
                }
                finally { mutex.ReleaseMutex(); }
            }
            string python = Path.Combine(install, "runtime", "python", "pythonw.exe");
            if (!File.Exists(python) || !File.Exists(Path.Combine(install, "bootstrap.py")))
                throw new IOException("运行环境文件缺失：" + install);
            string extra = Array.IndexOf(args, "--startup-test") >= 0 ? " --startup-test" : "";
            Process process = Process.Start(new ProcessStartInfo(python, "-B \"" + Path.Combine(install, "bootstrap.py") + "\"" + extra) {
                WorkingDirectory = install,
                UseShellExecute = false,
                CreateNoWindow = true
            });
            if (extra.Length != 0)
            {
                process.WaitForExit();
                Environment.ExitCode = process.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "漫画超分启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Environment.ExitCode = 1;
        }
    }

    static string HashFile(string path)
    {
        using (SHA256 hash = SHA256.Create())
        using (FileStream stream = File.OpenRead(path))
            return BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }

    static void DownloadPayload(string parent, string payload, Label label)
    {
        string[] hashes = PayloadParts.Split(',');
        if (hashes.Length == 0 || hashes[0].Length != 64)
            throw new IOException("运行库清单无效，请重新下载启动程序。");
        string cache = Path.Combine(parent, ".downloads");
        Directory.CreateDirectory(cache);
        System.Net.ServicePointManager.SecurityProtocol = System.Net.SecurityProtocolType.Tls12;
        using (FileStream output = File.Create(payload))
        {
            for (int index = 0; index < hashes.Length; index++)
            {
                string name = "MangaUpscale-" + Version + "-runtime.7z." + (index + 1).ToString("000");
                string part = Path.Combine(cache, hashes[index]);
                if (!File.Exists(part) || HashFile(part) != hashes[index])
                {
                    string local = Path.Combine(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location), name);
                    if (File.Exists(local)) File.Copy(local, part, true);
                    else
                    {
                        string partial = part + ".partial";
                        bool completed = false;
                        Exception failure = null;
                        using (System.Net.WebClient client = new System.Net.WebClient())
                        {
                            client.Headers["User-Agent"] = "MangaUpscale/" + Version;
                            client.DownloadProgressChanged += delegate(object sender, System.Net.DownloadProgressChangedEventArgs progress)
                            {
                                label.Text = "首次下载运行环境 (" + (index + 1) + "/" + hashes.Length + ")：" + progress.ProgressPercentage + "%\n完成后自动打开，之后可离线使用。";
                            };
                            client.DownloadFileCompleted += delegate(object sender, System.ComponentModel.AsyncCompletedEventArgs result)
                            {
                                failure = result.Error;
                                if (result.Cancelled) failure = new IOException("下载已取消。");
                                completed = true;
                            };
                            client.DownloadFileAsync(new Uri("https://github.com/Duke486/MangaUpscale/releases/download/v" + Version + "/" + name), partial);
                            while (!completed) { Application.DoEvents(); Thread.Sleep(50); }
                        }
                        if (failure != null) throw new IOException("运行环境下载失败，请检查网络后重新打开。已完成部分会保留。", failure);
                        if (File.Exists(part)) File.Delete(part);
                        File.Move(partial, part);
                    }
                    if (HashFile(part) != hashes[index])
                    {
                        File.Delete(part);
                        throw new IOException("运行库校验失败，请重新打开以重试下载。");
                    }
                }
                using (FileStream input = File.OpenRead(part)) input.CopyTo(output);
                Application.DoEvents();
            }
        }
        if (HashFile(payload) != PayloadHash) throw new IOException("运行环境完整性校验失败。");
        foreach (string hash in hashes) File.Delete(Path.Combine(cache, hash));
    }

    static void Install(string parent, string install, Label label)
    {
        Directory.CreateDirectory(parent);
        if (new DriveInfo(Path.GetPathRoot(parent)).AvailableFreeSpace < 11L * 1024 * 1024 * 1024)
            throw new IOException("首次解包需要至少 11 GB 可用磁盘空间。");
        string staging = Path.Combine(parent, ".install-" + Guid.NewGuid().ToString("N"));
        string tools = Path.Combine(staging, ".tools");
        Directory.CreateDirectory(tools);
        try
        {
            string payload = Path.Combine(tools, "payload.7z");
            using (FileStream source = File.OpenRead(Assembly.GetExecutingAssembly().Location))
            {
                source.Seek(-8, SeekOrigin.End);
                byte[] length = new byte[8];
                if (source.Read(length, 0, 8) != 8) throw new IOException("安装包已损坏。");
                long count = BitConverter.ToInt64(length, 0);
                if (count < 0 || count > source.Length - 8) throw new IOException("安装包长度无效。");
                if (count == 0) DownloadPayload(parent, payload, label);
                else
                {
                    source.Seek(-8 - count, SeekOrigin.End);
                    byte[] buffer = new byte[1024 * 1024];
                    using (SHA256 hash = SHA256.Create())
                    using (FileStream target = File.Create(payload))
                    {
                        while (count > 0)
                        {
                            int read = source.Read(buffer, 0, (int)Math.Min(buffer.Length, count));
                            if (read == 0) throw new IOException("安装包未下载完整。");
                            target.Write(buffer, 0, read);
                            hash.TransformBlock(buffer, 0, read, null, 0);
                            count -= read;
                            Application.DoEvents();
                        }
                        hash.TransformFinalBlock(new byte[0], 0, 0);
                        if (BitConverter.ToString(hash.Hash).Replace("-", "").ToLowerInvariant() != PayloadHash)
                            throw new IOException("安装包校验失败，请重新下载。");
                    }
                }
            }
            foreach (string name in new[] { "7z.exe", "7z.dll" })
            {
                using (Stream source = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
                using (FileStream target = File.Create(Path.Combine(tools, name)))
                    source.CopyTo(target);
            }
            label.Text = "正在展开 Python、显卡运行库和模型…\n完成后自动打开漫画超分。";
            Process process = Process.Start(new ProcessStartInfo(Path.Combine(tools, "7z.exe"),
                "x \"" + payload + "\" -o\"" + staging + "\" -y -mmt=2") {
                UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardOutput = true, RedirectStandardError = true
            });
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            while (!process.WaitForExit(100)) Application.DoEvents();
            if (process.ExitCode != 0) throw new IOException("运行环境解包失败，错误码：" + process.ExitCode);
            File.WriteAllText(Path.Combine(staging, "installed.ok"), PayloadHash);
            Directory.Delete(tools, true);
            Directory.Move(staging, install);
        }
        finally
        {
            if (Directory.Exists(staging)) Directory.Delete(staging, true);
        }
    }
}
