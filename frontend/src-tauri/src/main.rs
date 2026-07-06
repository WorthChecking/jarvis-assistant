use tauri::{Manager, Position, PhysicalPosition, Window};
use std::fs;
use std::path::PathBuf;
use std::os::windows::process::{CommandExt, ExitStatusExt};
use serde::Serialize;

fn position_file() -> PathBuf {
    tauri::api::path::app_data_dir(&tauri::Config::default())
        .unwrap_or_else(|| std::env::current_dir().unwrap())
        .join("jarvis_window_pos.json")
}

fn save_position(x: f64, y: f64) {
    let _ = fs::write(
        position_file(),
        format!(r#"{{"x":{},"y":{}}}"#, x as i32, y as i32),
    );
}

fn load_position() -> Option<(f64, f64)> {
    let data = fs::read_to_string(position_file()).ok()?;
    let v: serde_json::Value = serde_json::from_str(&data).ok()?;
    Some((v["x"].as_f64()?, v["y"].as_f64()?))
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            set_ignore_cursor_events,
            get_window_position,
            set_window_position,
            save_window_position,
            exec_system_action,
        ])
        .setup(|app| {
            let window = app.get_window("main").expect("主窗口不存在");

            // 恢复上次位置（如果存在）
            if let Some((x, y)) = load_position() {
                let _ = window.set_position(Position::Physical(PhysicalPosition { x: x as i32, y: y as i32 }));
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("启动 Tauri 失败");
}

#[tauri::command]
fn set_ignore_cursor_events(_app: tauri::AppHandle, _ignore: bool) -> Result<(), String> {
    // 不再穿透，所有状态均可交互
    Ok(())
}

#[tauri::command]
fn get_window_position(app: tauri::AppHandle) -> Result<(i32, i32), String> {
    let window = app.get_window("main").ok_or("主窗口不存在")?;
    let pos = window.outer_position()
        .map_err(|e| format!("获取位置失败: {:?}", e))?;
    Ok((pos.x, pos.y))
}

#[tauri::command]
fn set_window_position(app: tauri::AppHandle, x: i32, y: i32) -> Result<(), String> {
    let window = app.get_window("main").ok_or("主窗口不存在")?;
    window.set_position(tauri::Position::Physical(PhysicalPosition { x, y }))
        .map_err(|e| format!("设置位置失败: {:?}", e))
}

#[tauri::command]
fn save_window_position(_app: tauri::AppHandle, x: f64, y: f64) -> Result<(), String> {
    save_position(x, y);
    Ok(())
}

// ============================================================
// 系统操作执行器 — 接收后端下发的指令，在 Windows 宿主机执行
// ============================================================

#[derive(Serialize)]
struct ExecResult {
    success: bool,
    action: String,
    result: Option<serde_json::Value>,
    error: Option<String>,
}

#[tauri::command]
async fn exec_system_action(
    app: tauri::AppHandle,
    action: String,
    params: serde_json::Value,
) -> Result<ExecResult, String> {
    let window = app.get_window("main").ok_or("主窗口不存在")?;

    match action.as_str() {
        // ---- 音量控制 ----
        "volume_up" => {
            let step = params.get("step").and_then(|v| v.as_i64()).unwrap_or(3) as i32;
            for _ in 0..step {
                send_vk_key(0xAF); // VK_VOLUME_UP
            }
            Ok(ExecResult { success: true, action: "volume_up".into(), result: Some(serde_json::json!({"steps": step})), error: None })
        }
        "volume_down" => {
            let step = params.get("step").and_then(|v| v.as_i64()).unwrap_or(3) as i32;
            for _ in 0..step {
                send_vk_key(0xAE); // VK_VOLUME_DOWN
            }
            Ok(ExecResult { success: true, action: "volume_down".into(), result: Some(serde_json::json!({"steps": step})), error: None })
        }
        "volume_set" => {
            let level = params.get("level").and_then(|v| v.as_i64()).unwrap_or(50) as i32;
            let level = level.clamp(0, 100);
            // 用 PowerShell + CoreAudio API 精确设置音量
            let ps_cmd = format!(
                r#"Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public class CA{{[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]interface IAV{{int SetMasterVolumeLevelScalar(float f,Guid g);int GetMute(ref bool b);}}[Guid("D666063F-1587-4E43-81F1-B948E807363F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]interface IMD{{int Activate(ref Guid id,int c,IntPtr p,[MarshalAs(UnmanagedType.IUnknown)]out object o);}}[Guid("0BD7A1BE-7A1A-44DB-8397-CC5397C7B570"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]interface IDE{{int GetDefaultAudioEndpoint(int d,int r,out IMD e);}}[ComImport,Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]class DE{{}}public static void Set(float v){{var e=(IDE)new DE();IMD d;e.GetDefaultAudioEndpoint(0,1,out d);Guid g=typeof(IAV).GUID;object o;d.Activate(ref g,1,IntPtr.Zero,out o);((IAV)o).SetMasterVolumeLevelScalar(v,Guid.Empty);}}}}'; [CA]::Set({0}); Write-Output 'OK'"#,
                level as f32 / 100.0
            );
            let output = std::process::Command::new("powershell.exe")
                .args(["-NoProfile", "-Command", &ps_cmd])
                .creation_flags(0x08000000)
                .output()
                .map_err(|e| format!("设置音量失败: {:?}", e))?;
            let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if stdout == "OK" {
                Ok(ExecResult { success: true, action: "volume_set".into(), result: Some(serde_json::json!({"level": level})), error: None })
            } else {
                // 降级：用 keybd_event 逐步调整
                let target_steps = (level as f32 / 2.0).round() as i32;
                for _ in 0..100 { send_vk_key(0xAE); }
                for _ in 0..target_steps { send_vk_key(0xAF); }
                Ok(ExecResult { success: true, action: "volume_set".into(), result: Some(serde_json::json!({"level": level, "method": "fallback"})), error: None })
            }
        }
        "volume_get" => {
            // 用 PowerShell + CoreAudio API 查询当前音量
            let ps_cmd = r#"Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public class CAG{{[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]interface IAV{{int GetMasterVolumeLevelScalar(ref float f);int GetMute(ref bool b);}}[Guid("D666063F-1587-4E43-81F1-B948E807363F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]interface IMD{{int Activate(ref Guid id,int c,IntPtr p,[MarshalAs(UnmanagedType.IUnknown)]out object o);}}[Guid("0BD7A1BE-7A1A-44DB-8397-CC5397C7B570"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]interface IDE{{int GetDefaultAudioEndpoint(int d,int r,out IMD e);}}[ComImport,Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]class DE{{}}public static float Get(){{var e=(IDE)new DE();IMD d;e.GetDefaultAudioEndpoint(0,1,out d);Guid g=typeof(IAV).GUID;object o;d.Activate(ref g,1,IntPtr.Zero,out o);float v=0;((IAV)o).GetMasterVolumeLevelScalar(ref v);return v;}}}}'; [Math]::Round([CAG]::Get()*100)"#;
            let output = std::process::Command::new("powershell.exe")
                .args(["-NoProfile", "-Command", ps_cmd])
                .creation_flags(0x08000000)
                .output()
                .map_err(|e| format!("查询音量失败: {:?}", e))?;
            let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
            let vol: i32 = stdout.parse().unwrap_or(-1);
            if vol >= 0 {
                Ok(ExecResult { success: true, action: "volume_get".into(), result: Some(serde_json::json!({"level": vol})), error: None })
            } else {
                Err(format!("查询音量失败: {}", String::from_utf8_lossy(&output.stderr).trim()))
            }
        }
        "volume_mute" => {
            send_media_key(&window, 0xAD)?; // VK_VOLUME_MUTE
            Ok(ExecResult { success: true, action: "volume_mute".into(), result: Some(serde_json::json!({})), error: None })
        }

        // ---- 媒体控制 ----
        "media_play_pause" => {
            send_media_key(&window, 0xB3)?; // VK_MEDIA_PLAY_PAUSE
            Ok(ExecResult { success: true, action: "media_play_pause".into(), result: Some(serde_json::json!({})), error: None })
        }
        "media_next" => {
            send_media_key(&window, 0xB0)?; // VK_MEDIA_NEXT_TRACK
            Ok(ExecResult { success: true, action: "media_next".into(), result: Some(serde_json::json!({})), error: None })
        }
        "media_prev" => {
            send_media_key(&window, 0xB1)?; // VK_MEDIA_PREV_TRACK
            Ok(ExecResult { success: true, action: "media_prev".into(), result: Some(serde_json::json!({})), error: None })
        }

        // ---- 窗口控制 ----
        "window_minimize" => {
            window.minimize().map_err(|e| format!("最小化失败: {:?}", e))?;
            Ok(ExecResult { success: true, action: "window_minimize".into(), result: Some(serde_json::json!({})), error: None })
        }
        "window_maximize" => {
            if window.is_maximized().unwrap_or(false) {
                window.unmaximize().map_err(|e| format!("取消最大化失败: {:?}", e))?;
            } else {
                window.maximize().map_err(|e| format!("最大化失败: {:?}", e))?;
            }
            Ok(ExecResult { success: true, action: "window_maximize".into(), result: Some(serde_json::json!({})), error: None })
        }

        // ---- 锁屏 ----
        "system_lock" => {
            std::process::Command::new("rundll32.exe")
                .args(["user32.dll", "LockWorkStation"])
                .spawn()
                .map_err(|e| format!("锁屏命令执行失败: {:?}", e))?;
            Ok(ExecResult { success: true, action: "system_lock".into(), result: Some(serde_json::json!({})), error: None })
        }

        // ---- 应用启动（含常见应用名映射）----
        "app_launch" => {
            let app_name = params.get("app_name").and_then(|v| v.as_str()).unwrap_or("");
            if app_name.is_empty() {
                return Err("app_name 参数缺失".into());
            }
            // 常见应用名 → 实际可执行命令映射
            let launch_cmd = match app_name.to_lowercase().as_str() {
                "microsoft edge" | "edge" | "edge浏览器" | "微软edge" => Some("msedge".to_string()),
                "google chrome" | "chrome" | "谷歌浏览器" | "chrome浏览器" => Some("chrome".to_string()),
                "firefox" | "火狐" | "火狐浏览器" => Some("firefox".to_string()),
                "notepad" | "记事本" => Some("notepad".to_string()),
                "explorer" | "文件资源管理器" | "资源管理器" | "文件管理器" => Some("explorer".to_string()),
                "qqmusic" | "qq音乐" | "QQ音乐" => Some("QQMusic".to_string()),
                "qq" | "腾讯qq" | "QQ" | "腾讯qq" => Some("QQ".to_string()),
                "wechat" | "微信" | "WeChat" => Some("WeChat".to_string()),
                "spotify" => Some("Spotify".to_string()),
                "vscode" | "visual studio code" | "vs code" | "代码编辑器" => Some("code".to_string()),
                "terminal" | "终端" | "命令行" | "powershell" | "cmd" => Some("cmd".to_string()),
                "calc" | "计算器" => Some("calc".to_string()),
                "paint" | "画图" | "画板" => Some("mspaint".to_string()),
                "word" | "microsoft word" | "word文档" => Some("winword".to_string()),
                "excel" | "microsoft excel" | "excel表格" => Some("excel".to_string()),
                "powerpoint" | "ppt" | "microsoft powerpoint" | "演示文稿" => Some("powerpnt".to_string()),
                "steam" => Some("steam".to_string()),
                "douyu" | "斗鱼" | "斗鱼直播" => Some("douyu".to_string()),
                "douyin" | "抖音" => Some("douyin".to_string()),
                "bilibili" | "哔哩哔哩" | "B站" | "b站" => Some("bilibili".to_string()),
                "netease_cloud_music" | "网易云音乐" | "网易云" | "云音乐" => Some("cloudmusic".to_string()),
                _ => None,
            };
            let actual_cmd = launch_cmd.unwrap_or_else(|| app_name.to_string());

            // 通用六级启动策略（无需硬编码应用路径）：
            // 1) Start-Process（PATH/注册表 App Paths 中的应用）
            // 2) 注册表 App Paths 搜索（Windows "运行"对话框的查找机制）
            // 3) 开始菜单 + 桌面快捷方式搜索（精确 → 模糊）
            // 4) 注册表卸载信息搜索（InstallLocation + DisplayName 模糊匹配）
            // 5) 所有盘符常见目录递归搜索 .exe（深度3层）
            // 6) 降级 cmd /C start
            let ps_cmd = format!(
                "$cmd='{0}'; $name='{1}'; $found=$false; \
                 \
                 function Try-Launch($path) {{ \
                   if($path -and (Test-Path $path)){{ Start-Process $path; return $true }} \
                   return $false \
                 }}; \
                 \
                 function Find-Lnk($lnks, $c, $n, $exact) {{ \
                   foreach($l in $lnks){{ \
                     $base=[System.IO.Path]::GetFileNameWithoutExtension($l.Name); \
                     if($exact){{ if($base -eq $c -or $base -eq $n){{ return $l }} }} \
                     else{{ if($base -like \"*$c*\" -or $base -like \"*$n*\"){{ return $l }} }} \
                   }}; return $null \
                 }}; \
                 \
                 try {{ Start-Process $cmd -ErrorAction Stop; $found=$true }} catch {{}}; \
                 \
                 if(-not $found){{ \
                   $exeName=\"$cmd.exe\"; \
                   $regPaths=@(\"HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\$exeName\",\
                     \"HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\App Paths\\$exeName\",\
                     \"HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\$exeName\"); \
                   foreach($r in $regPaths){{ \
                     if(Test-Path $r){{ \
                       $p=(Get-ItemProperty $r).'(default)'; \
                       if(Try-Launch $p){{ $found=$true; break }} \
                     }} \
                   }} \
                 }}; \
                 \
                 if(-not $found){{ \
                   $dirs=@(\"$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs\",\
                     \"$env:ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\",\
                     \"$env:USERPROFILE\\Desktop\", \"$env:PUBLIC\\Desktop\"); \
                   $allLnks=$dirs | ForEach-Object {{ Get-ChildItem $_ -Filter *.lnk -Recurse -ErrorAction SilentlyContinue }}; \
                   $lnk=Find-Lnk $allLnks $cmd $name $true; \
                   if(-not $lnk){{ $lnk=Find-Lnk $allLnks $cmd $name $false }}; \
                   if($lnk){{ Start-Process $lnk.FullName; $found=$true }} \
                 }}; \
                 \
                 if(-not $found){{ \
                   $uninstKeys=@(\"HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*\",\
                     \"HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*\",\
                     \"HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*\"); \
                   foreach($uk in $uninstKeys){{ \
                     $app=Get-ItemProperty $uk -ErrorAction SilentlyContinue; \
                     if($app.DisplayName -and ($app.DisplayName -like \"*$name*\" -or $app.DisplayName -like \"*$cmd*\")){{ \
                       if($app.InstallLocation -and (Test-Path $app.InstallLocation)){{ \
                         $exe=Get-ChildItem $app.InstallLocation -Filter \"$cmd.exe\" -Recurse -Depth 2 -ErrorAction SilentlyContinue | Select-Object -First 1; \
                         if(-not $exe){{ $exe=Get-ChildItem $app.InstallLocation -Filter *.exe -Recurse -Depth 1 -ErrorAction SilentlyContinue | Where-Object {{ $_.Name -like \"*$cmd*\" }} | Select-Object -First 1 }}; \
                         if($exe){{ Start-Process $exe.FullName; $found=$true; break }} \
                       }} \
                     }} \
                   }} \
                 }}; \
                 \
                 if(-not $found){{ \
                   $drives=Get-PSDrive -PSProvider FileSystem | Where-Object {{ $_.Used -gt 0 }} | Select-Object -ExpandProperty Root; \
                   $exeName=\"$cmd.exe\"; \
                   foreach($drv in $drives){{ \
                     $f=Get-ChildItem $drv -Filter $exeName -Recurse -Depth 4 -ErrorAction SilentlyContinue | Select-Object -First 1; \
                     if($f){{ Start-Process $f.FullName; $found=$true; break }} \
                   }} \
                 }}; \
                 \
                 if(-not $found){{ cmd /C start '' $cmd 2>$null; if($?){{ $found=$true }} }}; \
                 if($found){{ Write-Output 'OK' }} else {{ Write-Error 'NOT_FOUND' }}",
                actual_cmd.replace("'", "''"),
                app_name.replace("'", "''")
            );
            // 使用 spawn_blocking 避免阻塞 Tauri 主线程（全盘搜索可能耗时较长）
            let output = tauri::async_runtime::spawn_blocking(move || {
                std::process::Command::new("powershell.exe")
                    .args(["-NoProfile", "-Command", &ps_cmd])
                    .creation_flags(0x08000000)
                    .output()
            })
            .await
            .map_err(|e| format!("应用启动线程异常: {:?}", e))?
            .map_err(|e| format!("应用启动失败 '{}': {:?}", actual_cmd, e))?;
            let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            if stdout != "OK" && !stderr.is_empty() {
                return Err(format!("应用启动失败: {} | {}", actual_cmd, stderr));
            }
            Ok(ExecResult { success: true, action: "app_launch".into(), result: Some(serde_json::json!({"app": app_name, "launched": actual_cmd})), error: None })
        }

        // ---- 应用关闭 ----
        "app_close" => {
            let app_name = params.get("app_name").and_then(|v| v.as_str()).unwrap_or("");
            if app_name.is_empty() {
                return Err("app_name 参数缺失".into());
            }
            // 应用名 → 进程名映射
            let process_name = match app_name.to_lowercase().as_str() {
                "microsoft edge" | "edge" | "edge浏览器" | "微软edge" => "msedge",
                "google chrome" | "chrome" | "谷歌浏览器" => "chrome",
                "firefox" | "火狐" | "火狐浏览器" => "firefox",
                "notepad" | "记事本" => "notepad",
                "qqmusic" | "qq音乐" | "QQ音乐" => "QQMusic",
                "qq" | "腾讯qq" | "QQ" => "QQ",
                "wechat" | "微信" | "WeChat" => "WeChat",
                "spotify" => "Spotify",
                "vscode" | "visual studio code" | "vs code" | "代码编辑器" => "Code",
                "calc" | "计算器" => "calc",
                "steam" => "steam",
                "bilibili" | "哔哩哔哩" | "b站" | "B站" => "bilibili",
                "douyu" | "斗鱼" | "斗鱼直播" => "douyu",
                "douyin" | "抖音" => "douyin",
                "netease_cloud_music" | "网易云音乐" | "网易云" | "云音乐" => "cloudmusic",
                _ => app_name,
            };
            // 使用 taskkill /F /IM 关闭进程（先尝试优雅关闭，失败则强制）
            let process_names = match app_name.to_lowercase().as_str() {
                "抖音" | "douyin" => vec!["douyin", "Aweme", "douyin_pc"],
                "哔哩哔哩" | "bilibili" | "B站" | "b站" => vec!["bilibili"],
                _ => vec![process_name],
            };
            let mut killed = false;
            let mut last_error = String::new();
            for pname in &process_names {
                // 先尝试优雅关闭
                let output = std::process::Command::new("taskkill.exe")
                    .args(["/IM", &format!("{}.exe", pname)])
                    .creation_flags(0x08000000)
                    .output()
                    .unwrap_or_else(|_| std::process::Output { status: std::process::ExitStatus::from_raw(1), stdout: Vec::new(), stderr: Vec::new() });
                if output.status.success() {
                    killed = true;
                    break;
                }
                // 强制关闭
                let force_output = std::process::Command::new("taskkill.exe")
                    .args(["/F", "/IM", &format!("{}.exe", pname)])
                    .creation_flags(0x08000000)
                    .output()
                    .unwrap_or_else(|_| std::process::Output { status: std::process::ExitStatus::from_raw(1), stdout: Vec::new(), stderr: Vec::new() });
                if force_output.status.success() {
                    killed = true;
                    break;
                }
                let stderr = String::from_utf8_lossy(&force_output.stderr).trim().to_string();
                if !stderr.contains("not found") && !stderr.contains("未找到") {
                    last_error = stderr;
                }
            }
            if !killed && !last_error.is_empty() {
                // 最后尝试 PowerShell 按窗口标题模糊匹配关闭
                let ps_cmd = format!(
                    "$procs = Get-Process | Where-Object {{ $_.MainWindowTitle -like '*{}*' }}; if($procs){{ $procs | Stop-Process -Force; Write-Output 'OK' }} else {{ Write-Error 'NOT_FOUND' }}",
                    app_name.replace("'", "''")
                );
                let ps_output = std::process::Command::new("powershell.exe")
                    .args(["-NoProfile", "-Command", &ps_cmd])
                    .creation_flags(0x08000000)
                    .output()
                    .map_err(|e| format!("PowerShell 关闭失败: {:?}", e))?;
                let ps_stdout = String::from_utf8_lossy(&ps_output.stdout).trim().to_string();
                if ps_stdout == "OK" {
                    killed = true;
                }
            }
            if killed {
                Ok(ExecResult { success: true, action: "app_close".into(), result: Some(serde_json::json!({"app": app_name, "killed": true})), error: None })
            } else {
                Err(format!("关闭应用失败: {} | 尝试进程: {}", app_name, process_names.join(", ")))
            }
        }

        // ---- 网页搜索 ----
        "web_search" => {
            let query = params.get("query").and_then(|v| v.as_str()).unwrap_or("");
            if query.is_empty() {
                return Err("搜索关键词缺失".into());
            }
            let ps_cmd = format!(
                "Start-Process 'https://www.bing.com/search?q={}'",
                query.replace("'", "''").replace(" ", "+")
            );
            let output = std::process::Command::new("powershell.exe")
                .args(["-NoProfile", "-Command", &ps_cmd])
                .creation_flags(0x08000000)
                .output()
                .map_err(|e| format!("打开浏览器失败: {:?}", e))?;
            if output.status.success() {
                Ok(ExecResult { success: true, action: "web_search".into(), result: Some(serde_json::json!({"query": query})), error: None })
            } else {
                Err(format!("打开浏览器失败: {}", String::from_utf8_lossy(&output.stderr).trim()))
            }
        }

        // ---- 打开网址 ----
        "open_url" => {
            let url = params.get("url").and_then(|v| v.as_str()).unwrap_or("");
            if url.is_empty() {
                return Err("网址缺失".into());
            }
            // 确保有协议前缀
            let full_url = if url.starts_with("http://") || url.starts_with("https://") {
                url.to_string()
            } else {
                format!("https://{}", url)
            };
            let ps_cmd = format!("Start-Process '{}'", full_url.replace("'", "''"));
            let output = std::process::Command::new("powershell.exe")
                .args(["-NoProfile", "-Command", &ps_cmd])
                .creation_flags(0x08000000)
                .output()
                .map_err(|e| format!("打开网址失败: {:?}", e))?;
            if output.status.success() {
                Ok(ExecResult { success: true, action: "open_url".into(), result: Some(serde_json::json!({"url": full_url})), error: None })
            } else {
                Err(format!("打开网址失败: {}", String::from_utf8_lossy(&output.stderr).trim()))
            }
        }

        _ => Err(format!("未知系统操作: {}", action)),
    }
}

/// 通过 PowerShell 发送媒体键（音量/播放控制）
/// 使用 WScript.Shell SendKeys 模拟键盘事件
fn send_vk_key(vk_code: u16) {
    // 用 Windows API keybd_event 模拟按键（不依赖窗口焦点）
    unsafe {
        extern "system" {
            fn keybd_event(bVk: u8, bScan: u8, dwFlags: u32, dwExtraInfo: usize);
        }
        const KEYEVENTF_KEYUP: u32 = 0x0002;
        keybd_event(vk_code as u8, 0, 0, 0); // 按下
        keybd_event(vk_code as u8, 0, KEYEVENTF_KEYUP, 0); // 释放
        std::thread::sleep(std::time::Duration::from_millis(30));
    }
}

fn send_media_key(_window: &Window, vk_code: u16) -> Result<(), String> {
    let key_seq: String = match vk_code {
        0xAD => "{VOLUME_MUTE}".into(),       // 静音
        0xAE => "{VOLUME_DOWN}".into(),        // 音量减
        0xAF => "{VOLUME_UP}".into(),          // 音量增
        0xB3 => "{MEDIA_PLAY_PAUSE}".into(),    // 播放/暂停
        0xB0 => "{MEDIA_NEXT}".into(),          // 下一首
        0xB1 => "{MEDIA_PREVIOUS}".into(),      // 上一首
        _ => return Err(format!("未知虚拟键码: {}", vk_code)),
    };
    std::process::Command::new("powershell.exe")
        .args(["-Command", &format!(
            "$w = New-Object -ComObject WScript.Shell; $w.SendKeys('{}')", key_seq
        )])
        .creation_flags(0x08000000) // CREATE_NO_WINDOW
        .output()
        .map_err(|e| format!("PowerShell 媒体键执行失败: {:?}", e))?;
    Ok(())
}
