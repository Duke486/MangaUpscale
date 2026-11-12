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
    const string Version = "APP_VERSION";
    const string PayloadHash = "PAYLOAD_HASH";

    [STAThread]
    static void Main(string[] args)
    {
        Application.EnableVisualStyles();
        try
        {
            string parent = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "MangaUpscale");
            string install = Path.Combine(parent, Version + "-client");
            using (Mutex mutex = new Mutex(false, "Local\\MangaUpscaleInstall" + Version))
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
                    if (!File.Exists(Path.Combine(install, "client-ready.ok")))
                    {
                        Process setup = Process.Start(new ProcessStartInfo(Path.Combine(install, "runtime", "python", "python.exe"),
                            "-u \"" + Path.Combine(install, "setup_client.py") + "\"") { WorkingDirectory = install, UseShellExecute = false });
                        setup.WaitForExit();
                        if (setup.ExitCode != 0) throw new IOException("首次安装失败，请检查网络后重新打开；已下载内容会保留。");
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
                if (count <= 0 || count > source.Length - 8) throw new IOException("安装包长度无效。");
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
            foreach (string name in new[] { "7z.exe", "7z.dll" })
            {
                using (Stream source = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
                using (FileStream target = File.Create(Path.Combine(tools, name)))
                    source.CopyTo(target);
            }
            label.Text = "正在展开应用代码和基础 Python…\n完成后继续安装运行环境。";
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
