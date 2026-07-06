[Console]::OutputEncoding=[System.Text.Encoding]::UTF8

$code = @"
using System;
using System.Runtime.InteropServices;
public class CoreAudio {
    [Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioEndpointVolume {
        int RegisterControlChangeNotify(IntPtr pNotify);
        int UnregisterControlChangeNotify(IntPtr pNotify);
        int GetChannelCount(ref uint pnChannelCount);
        int SetMasterVolumeLevel(float fLevelDB, IntPtr pguidEventContext);
        int SetMasterVolumeLevelScalar(float fLevel, IntPtr pguidEventContext);
        int GetMasterVolumeLevel(ref float pfLevelDB);
        int GetMasterVolumeLevelScalar(ref float pfLevel);
        int SetChannelVolumeLevel(uint nChannel, float fLevelDB, IntPtr pguidEventContext);
        int SetChannelVolumeLevelScalar(uint nChannel, float fLevel, IntPtr pguidEventContext);
        int GetChannelVolumeLevel(uint nChannel, ref float pfLevelDB);
        int GetChannelVolumeLevelScalar(uint nChannel, ref float pfLevel);
        int SetMute(bool bMute, IntPtr pguidEventContext);
        int GetMute(ref bool pbMute);
        int GetVolumeStepInfo(ref uint pnStep, ref uint pnStepCount);
        int QueryHardwareSupport(ref uint pdwHardwareSupportMask);
        int GetVolumeRange(ref float pfMin, ref float pfMax, ref float pfIncrement);
    }
    [Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDevice {
        int Activate(ref Guid id, int clsCtx, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
    }
    [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IMMDeviceEnumerator {
        int EnumAudioEndpoints(int dataFlow, int stateMask, out IntPtr collection);
        int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint);
    }

    static IAudioEndpointVolume GetEndpointVolume() {
        Guid clsid = new Guid("BCDE0395-E52F-467C-8E3D-C4579291692E");
        var type = Type.GetTypeFromCLSID(clsid);
        if (type == null) throw new Exception("MMDeviceEnumerator not found");
        var obj = Activator.CreateInstance(type);
        IntPtr pUnk = Marshal.GetIUnknownForObject(obj);
        Guid iid = typeof(IMMDeviceEnumerator).GUID;
        IntPtr pEnum;
        int hr = Marshal.QueryInterface(pUnk, ref iid, out pEnum);
        Marshal.Release(pUnk);
        if (hr != 0 || pEnum == IntPtr.Zero) throw new Exception("QueryInterface IMMDeviceEnumerator failed: 0x" + hr.ToString("X"));
        var en = (IMMDeviceEnumerator)Marshal.GetObjectForIUnknown(pEnum);
        IMMDevice dev;
        hr = en.GetDefaultAudioEndpoint(0, 1, out dev);
        if (hr != 0) throw new Exception("GetDefaultAudioEndpoint failed: 0x" + hr.ToString("X"));
        Guid iidVol = typeof(IAudioEndpointVolume).GUID;
        object iface;
        hr = dev.Activate(ref iidVol, 1, IntPtr.Zero, out iface);
        if (hr != 0) throw new Exception("Activate IAudioEndpointVolume failed: 0x" + hr.ToString("X"));
        return (IAudioEndpointVolume)iface;
    }

    public static int Get() {
        try {
            var vol = GetEndpointVolume();
            float v = 0;
            int hr = vol.GetMasterVolumeLevelScalar(ref v);
            if (hr != 0) return -1;
            return (int)Math.Round(v * 100);
        } catch { return -1; }
    }

    public static int Set(int target) {
        try {
            target = Math.Max(0, Math.Min(100, target));
            var vol = GetEndpointVolume();
            float scalar = target / 100.0f;
            int hr = vol.SetMasterVolumeLevelScalar(scalar, IntPtr.Zero);
            if (hr != 0) return -1;
            // Read back actual value
            float actual = 0;
            vol.GetMasterVolumeLevelScalar(ref actual);
            return (int)Math.Round(actual * 100);
        } catch { return -1; }
    }
}
"@

Add-Type -TypeDefinition $code

$cmd = $args[0]

if ($cmd -eq "get") {
    $val = [CoreAudio]::Get()
    Write-Output $val
} elseif ($cmd -eq "set") {
    $target = [int]$args[1]
    $actual = [CoreAudio]::Set($target)
    Write-Output "OK:$actual"
}
