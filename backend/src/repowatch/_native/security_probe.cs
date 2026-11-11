// Fixed, non-secret native admission canaries. Never enumerate credentials.
using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;

public static class RepoWatchNativeSecurityCanary {
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool CredRead(string target,uint type,uint flags,out IntPtr credential);
    [DllImport("advapi32.dll")] static extern void CredFree(IntPtr credential);
    static string Connect(int port) {
        using(TcpClient client=new TcpClient()) {
            try {
                var connection=client.ConnectAsync(IPAddress.Loopback,port);
                return connection.Wait(1000) ? "connected" : "bounded_timeout";
            } catch(Exception e) {
                while(e.InnerException!=null) e=e.InnerException;
                SocketException socket=e as SocketException;
                return socket!=null && socket.SocketErrorCode==SocketError.AccessDenied ? "denied" : "unverified";
            }
        }
    }
    public static int Main(string[] args) {
        if((args.Length!=5 && args.Length!=6) || !args[1].StartsWith("RepoWatch.NativeCanary.",StringComparison.Ordinal)) return 120;
        bool credentialReadable=false; int credentialError=0; IntPtr pointer;
        if(CredRead(args[1],1,0,out pointer)) { credentialReadable=true; CredFree(pointer); }
        else credentialError=Marshal.GetLastWin32Error();
        bool environmentPresent=Environment.GetEnvironmentVariable(args[3])!=null;
        string network=Connect(int.Parse(args[2]));
        if(args[0]=="baseline") return credentialReadable && environmentPresent && network=="connected" ? 0 : 121;
        if(args[0]!="isolated") return 122;
        string control=args.Length==6 ? Connect(int.Parse(args[5])) : "not_run";
        bool credentialDenied=!credentialReadable && (credentialError==5 || credentialError==1168 || credentialError==1312);
        // A bounded timeout is a connectivity observation, not an explicit denial.
        // The parent must also verify no accepted connection and a live before/after baseline.
        bool passed=credentialDenied && !environmentPresent && (network=="denied" || network=="bounded_timeout")
            && (control=="not_run" || control=="denied" || control=="bounded_timeout");
        string report="{\"credential_canary_read_denied\":"+credentialDenied.ToString().ToLowerInvariant()
            +",\"credential_error\":"+credentialError+",\"parent_environment_absent\":"+(!environmentPresent).ToString().ToLowerInvariant()
            +",\"loopback_connection\":\""+network+"\",\"real_control_api_connection\":\""+control+"\",\"passed\":"+passed.ToString().ToLowerInvariant()+"}";
        File.WriteAllText(args[4],report);
        return passed ? 0 : 123;
    }
}
