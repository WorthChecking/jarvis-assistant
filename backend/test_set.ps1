[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$code = @"
using System;
using System.Runtime.InteropServices;
public class CoreAudio {
    [Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioEndpointVolume {
        // vtable 3
        int RegisterControlChangeNotify(IntPtr pNotify);
        // vtable 4
        int UnregisterControlChangeNotify(IntPtr pNotify);
        // vtable 5
        int GetChannelCount(ref uint pnChannelCount);
        // vtable 6
        [PreserveSig]
        int SetMasterVolumeLevel(float fLevelDB, IntPtr pguidEventContext);
        // vtable 7
        [PreserveSig]
        int GetMasterVolumeLevel(ref float pfLevelDB);
        // vtable 8
        [PreserveSig]
        int SetMasterVolumeLevelScalar(float fLevel, IntPtr pguidEventContext);
        // vtable 9
        [PreserveSig]
        int GetMasterVolumeLevelScalar(ref float pfLevel);
        // vtable 10
        [PreserveSig]
        int SetChannelVolumeLevel(uint nChannel, float fLevelDB, IntPtr pguidEventContext);
        // vtable 11
        [PreserveSig]
        int GetChannelVolumeLevel(uint nChannel, ref float pfLevelDB);
        // vtable 12
        [PreserveSig]
        int SetChannelVolumeLevelScalar(uint nChannel, float fLevel, IntPtr pguidEventContext);
        // vtable 13
        [PreserveSig]
        int GetChannelVolumeLevelScalar(uint nChannel, ref float pfLevel);
        // vtable 14
        [PreserveSig]
        int SetMute(int bMute, IntPtr pguidEventContext);
        // vtable 15
        [PreserveSig]
        int GetMute(ref int pbMute);
        // vtable 16
        [PreserveSig]
        int GetVolumeStepInfo(ref uint pnStep, ref uint pnStepCount);
        // vtable 17
        [PreserveSig]
        int QueryHardwareSupport(ref uint pdwHardwareSupportMask);
        // vtable 18
        [PreserveSig]
        int GetVolumeRange(ref float pflVolumeMindB, ref float pflVolumeMaxdB, ref float pflVolumeIncrementdB);
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

    static IAudioEndpointVolume GetVolumeControl() {
        Guid clsid = new Guid("BCDE0395-E52F-467C-8E3D-C4579291692E");
        var type = Type.GetTypeFromCLSID(clsid);
        var obj = Activator.CreateInstance(type);
        IntPtr pUnk = Marshal.GetIUnknownForObject(obj);
        Guid iid = typeof(IMMDeviceEnumerator).GUID;
        IntPtr pEnum;
        Marshal.QueryInterface(pUnk, ref iid, out pEnum);
        var en = (IMMDeviceEnumerator)Marshal.GetObjectForIUnknown(pEnum);
        Marshal.Release(pUnk);
        IMMDevice dev; en.GetDefaultAudioEndpoint(0, 1, out dev);
        Guid iidVol = typeof(IAudioEndpointVolume).GUID;
        object iface; dev.Activate(ref iidVol, 23, IntPtr.Zero, out iface);
        return (IAudioEndpointVolume)iface;
    }

    public static string Info() {
        var vol = GetVolumeControl();
        float minDB = 0, maxDB = 0, incDB = 0;
        vol.GetVolumeRange(ref minDB, ref maxDB, ref incDB);
        uint step = 0, stepCount = 0;
        vol.GetVolumeStepInfo(ref step, ref stepCount);
        float scalar = 0;
        vol.GetMasterVolumeLevelScalar(ref scalar);
        return "minDB=" + minDB + " maxDB=" + maxDB + " incDB=" + incDB + " step=" + step + "/" + stepCount + " scalar=" + scalar;
    }

    public static string SetPct(float pct) {
        var vol = GetVolumeControl();
        float minDB = 0, maxDB = 0, incDB = 0;
        vol.GetVolumeRange(ref minDB, ref maxDB, ref incDB);
        float db;
        if (pct <= 0) { db = minDB; }
        else if (pct >= 100) { db = maxDB; }
        else {
            // logarithmic: dB = 20 * log10(scalar)
            float scalar = pct / 100.0f;
            db = (float)(20.0 * Math.Log10(scalar));
            if (db < minDB) db = minDB;
        }
        int hr = vol.SetMasterVolumeLevel(db, IntPtr.Zero);
        float check = 0;
        vol.GetMasterVolumeLevelScalar(ref check);
        return "HR=0x" + hr.ToString("X") + " db=" + db + " got=" + Math.Round(check * 100);
    }
}
"@
Add-Type -TypeDefinition $code
Write-Output ([CoreAudio]::Info())
