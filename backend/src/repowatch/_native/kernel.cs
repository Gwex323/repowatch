// Native isolation kernel. Admission, workspace and task lifecycle remain host responsibilities.
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using System.IO;

public static class RepoWatchNativeKernel {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct STARTUPINFO {
        public int cb; public string reserved, desktop, title;
        public int x,y,xSize,ySize,xCount,yCount,fill,flags;
        public short show,reservedSize; public IntPtr reservedBytes,input,output,error;
    }
    [StructLayout(LayoutKind.Sequential)] struct STARTUPINFOEX {
        public STARTUPINFO startup; public IntPtr attributes;
    }
    [StructLayout(LayoutKind.Sequential)] struct PROCESS_INFORMATION {
        public IntPtr process,thread; public uint pid,tid;
    }
    [StructLayout(LayoutKind.Sequential)] struct SECURITY_CAPABILITIES {
        public IntPtr sid,capabilities; public uint count,reserved;
    }
    [StructLayout(LayoutKind.Sequential)] struct BASIC_LIMIT {
        public long processTime,jobTime; public uint flags;
        public UIntPtr minWorking,maxWorking; public uint activeLimit;
        public UIntPtr affinity; public uint priority,scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct IO_COUNTERS {
        public ulong readOperations,writeOperations,otherOperations,readBytes,writeBytes,otherBytes;
    }
    [StructLayout(LayoutKind.Sequential)] struct EXTENDED_LIMIT {
        public BASIC_LIMIT basic; public IO_COUNTERS io;
        public UIntPtr processMemory,jobMemory,peakProcess,peakJob;
    }
    [StructLayout(LayoutKind.Sequential)] struct ACCOUNTING {
        public long user,kernel,periodUser,periodKernel;
        public uint faults,total,active,terminated;
    }
    [StructLayout(LayoutKind.Sequential)] struct SECURITY_ATTRIBUTES {
        public int length; public IntPtr descriptor; public int inherit;
    }
    [StructLayout(LayoutKind.Sequential)] struct CPU_RATE { public uint flags,rate; }
    public sealed class Options {
        public string ProgressPath,JobName,GatePath,CancelPath,OutputPath,ControllerSid;
    }
    public sealed class Result {
        public int ExitCode=-1;
        public bool TimedOut,Cancelled,LogTruncated;
        public uint ProcessId;
        public long Created;
        public int ActiveAfterStop;
        public byte[] Output;
    }
    [DllImport("userenv.dll", CharSet=CharSet.Unicode)] static extern int CreateAppContainerProfile(string name,string display,string description,IntPtr caps,uint count,out IntPtr sid);
    [DllImport("userenv.dll", CharSet=CharSet.Unicode)] static extern int DeleteAppContainerProfile(string name);
    [DllImport("userenv.dll", CharSet=CharSet.Unicode)] static extern int DeriveAppContainerSidFromAppContainerName(string name,out IntPtr sid);
    [DllImport("advapi32.dll")] static extern IntPtr FreeSid(IntPtr sid);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool InitializeProcThreadAttributeList(IntPtr list,int count,int flags,ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool UpdateProcThreadAttribute(IntPtr list,uint flags,IntPtr attribute,IntPtr value,IntPtr size,IntPtr previous,IntPtr returnSize);
    [DllImport("kernel32.dll")] static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool CreateProcess(string app,StringBuilder command,IntPtr processAttributes,IntPtr threadAttributes,bool inherit,uint flags,IntPtr environment,string directory,ref STARTUPINFOEX startup,out PROCESS_INFORMATION process);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr attributes,string name);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr OpenJobObject(uint access,bool inherit,string name);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr job,int type,out ACCOUNTING value,int length,IntPtr needed);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateJobObject(IntPtr job,uint code);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int type,ref EXTENDED_LIMIT value,int length);
    [DllImport("kernel32.dll", EntryPoint="SetInformationJobObject", SetLastError=true)] static extern bool SetCpuRate(IntPtr job,int type,ref CPU_RATE value,int length);
    [DllImport("kernel32.dll")] static extern void SetLastError(uint code);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr CreateFile(string name,uint access,uint share,ref SECURITY_ATTRIBUTES attributes,uint disposition,uint flags,IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetFileSizeEx(IntPtr file,out long size);
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool ConvertStringSecurityDescriptorToSecurityDescriptor(string text,uint revision,out IntPtr descriptor,out uint size);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr value);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr handle,uint milliseconds);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr handle,out uint code);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetProcessTimes(IntPtr handle,out long created,out long exited,out long kernel,out long user);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateProcess(IntPtr process,uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr process,uint access,out IntPtr token);
    [DllImport("advapi32.dll", SetLastError=true)] static extern bool GetTokenInformation(IntPtr token,int type,out int value,int length,out int needed);

    public sealed class Profile : IDisposable {
        public IntPtr Sid; public string Name;
        bool ownsProfile=true;
        Profile() { }
        public Profile(string name) {
            Name=name;
            int hr=CreateAppContainerProfile(name,name,"RepoWatch isolated task",IntPtr.Zero,0,out Sid);
            if(hr<0) Marshal.ThrowExceptionForHR(hr);
        }
        public static Profile OpenExisting(string name) {
            IntPtr sid; int hr=DeriveAppContainerSidFromAppContainerName(name,out sid);
            if(hr<0) Marshal.ThrowExceptionForHR(hr);
            return new Profile {Sid=sid,Name=name,ownsProfile=false};
        }
        public void Dispose() {
            if(Sid!=IntPtr.Zero) { FreeSid(Sid); Sid=IntPtr.Zero; }
            if(ownsProfile) {
                ownsProfile=false;
                int hr=DeleteAppContainerProfile(Name);
                if(hr<0) Marshal.ThrowExceptionForHR(hr);
            }
        }
    }
    static void Check(bool ok,string stage="native API") { if(!ok) throw new Win32Exception(Marshal.GetLastWin32Error(),stage); }
    public static uint LastProcessId;
    public static long LastCreationTime;
    public static int LastJobActiveBeforeStop,LastJobActiveAfterStop,LastJobTotalProcesses;
    public static long CurrentCreated() {
        long created,exited,kernel,user;
        Check(GetProcessTimes(new IntPtr(-1),out created,out exited,out kernel,out user),"Current process identity");
        return created;
    }
    static ACCOUNTING Counts(IntPtr job) {
        ACCOUNTING counts; Check(QueryInformationJobObject(job,1,out counts,Marshal.SizeOf(typeof(ACCOUNTING)),IntPtr.Zero),"Job accounting"); return counts;
    }
    public static int[] JobCounts(string name) {
        IntPtr job=OpenJobObject(4,false,name);
        if(job==IntPtr.Zero) {
            int error=Marshal.GetLastWin32Error();
            if(error==2) return null;
            throw new Win32Exception(error,"OpenJobObject");
        }
        try { ACCOUNTING value=Counts(job); return new int[]{(int)value.active,(int)value.total}; }
        finally { CloseHandle(job); }
    }

    // No inherited handles, no network capabilities, an explicit sanitized environment,
    // suspended creation, token verification, job assignment, then the startup gate.
    public static int Run(Profile profile,string executable,string command,string directory,string environment,int timeoutMs) {
        return RunOwned(profile,executable,command,directory,environment,timeoutMs,null,null);
    }
    public static int RunOwned(Profile profile,string executable,string command,string directory,string environment,int timeoutMs,string progressPath,string jobName) {
        return Core(profile,executable,command,directory,environment,timeoutMs,progressPath,jobName,null);
    }
    public static string CommandLine(string[] arguments) {
        StringBuilder result=new StringBuilder();
        foreach(string argument in arguments) {
            if(argument==null || argument.IndexOf('\0')>=0) throw new ArgumentException("Invalid argument");
            if(result.Length>0) result.Append(' ');
            result.Append('"'); int slashes=0;
            foreach(char value in argument) {
                if(value=='\\') {slashes++;continue;}
                if(value=='"') {result.Append('\\',slashes*2+1);result.Append('"');}
                else {result.Append('\\',slashes);result.Append(value);}
                slashes=0;
            }
            result.Append('\\',slashes*2); result.Append('"');
        }
        return result.ToString();
    }
    public static Result Execute(Profile profile,string executable,string command,string directory,string environment,int timeoutMs,Options options) {
        if(options==null || String.IsNullOrEmpty(options.OutputPath) || String.IsNullOrEmpty(options.JobName)) throw new ArgumentException("Explicit output and job identity required");
        Result result=new Result();
        try { result.ExitCode=Core(profile,executable,command,directory,environment,timeoutMs,options.ProgressPath,options.JobName,options); }
        catch(TimeoutException) { result.TimedOut=true; }
        catch(OperationCanceledException) { result.Cancelled=true; }
        result.ProcessId=LastProcessId; result.Created=LastCreationTime; result.ActiveAfterStop=LastJobActiveAfterStop;
        if(result.ActiveAfterStop!=0) throw new InvalidOperationException("Native job stop unverified");
        using(FileStream stream=new FileStream(options.OutputPath,FileMode.Open,FileAccess.Read,FileShare.Read)) {
            result.LogTruncated=stream.Length>8388608;
            result.Output=new byte[(int)Math.Min(stream.Length,8388608)];
            int read=0; while(read<result.Output.Length) {int count=stream.Read(result.Output,read,result.Output.Length-read);if(count==0)throw new EndOfStreamException();read+=count;}
        }
        return result;
    }
    static int Core(Profile profile,string executable,string command,string directory,string environment,int timeoutMs,string progressPath,string jobName,Options options) {
        if(timeoutMs<1 || timeoutMs>3600000) throw new ArgumentOutOfRangeException("timeoutMs");
        if(jobName!=null && !jobName.StartsWith(options==null ? "Local\\RepoWatchProbe-" : "Local\\RepoWatchNative-",StringComparison.Ordinal)) throw new ArgumentException("Unexpected job name");
        LastJobActiveBeforeStop=LastJobActiveAfterStop=LastJobTotalProcesses=-1;
        IntPtr size=IntPtr.Zero,attributes=IntPtr.Zero,capPtr=IntPtr.Zero,envPtr=IntPtr.Zero,job=IntPtr.Zero,token=IntPtr.Zero;
        IntPtr handles=IntPtr.Zero,output=IntPtr.Zero,input=IntPtr.Zero,jobDescriptor=IntPtr.Zero,jobAttributes=IntPtr.Zero;
        bool initialized=false,created=false;
        PROCESS_INFORMATION process=new PROCESS_INFORMATION();
        try {
            int attributeCount=options==null ? 1 : 2;
            InitializeProcThreadAttributeList(IntPtr.Zero,attributeCount,0,ref size);
            attributes=Marshal.AllocHGlobal(size);
            Check(InitializeProcThreadAttributeList(attributes,attributeCount,0,ref size)); initialized=true;
            SECURITY_CAPABILITIES caps=new SECURITY_CAPABILITIES(); caps.sid=profile.Sid;
            capPtr=Marshal.AllocHGlobal(Marshal.SizeOf(typeof(SECURITY_CAPABILITIES)));
            Marshal.StructureToPtr(caps,capPtr,false);
            Check(UpdateProcThreadAttribute(attributes,0,new IntPtr(0x20009),capPtr,new IntPtr(Marshal.SizeOf(caps)),IntPtr.Zero,IntPtr.Zero));
            if(options!=null) {
                string controller=new System.Security.Principal.SecurityIdentifier(options.ControllerSid).Value;
                string owner=System.Security.Principal.WindowsIdentity.GetCurrent().User.Value;
                uint descriptorSize;
                Check(ConvertStringSecurityDescriptorToSecurityDescriptor("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;"+owner+")(A;;0x10000C;;;"+controller+")",1,out jobDescriptor,out descriptorSize),"Job security descriptor");
                SECURITY_ATTRIBUTES jobSecurity=new SECURITY_ATTRIBUTES(); jobSecurity.length=Marshal.SizeOf(jobSecurity);jobSecurity.descriptor=jobDescriptor;
                jobAttributes=Marshal.AllocHGlobal(Marshal.SizeOf(jobSecurity));Marshal.StructureToPtr(jobSecurity,jobAttributes,false);
            }
            SetLastError(0);
            job=CreateJobObject(jobAttributes,jobName); Check(job!=IntPtr.Zero);
            if(jobName!=null && Marshal.GetLastWin32Error()==183) throw new InvalidOperationException("Named job already exists");
            EXTENDED_LIMIT limit=new EXTENDED_LIMIT(); limit.basic.flags=0x2000; // KILL_ON_JOB_CLOSE, no breakaway.
            if(options!=null) {
                limit.basic.flags|=0x308;limit.basic.activeLimit=128;
                limit.processMemory=new UIntPtr(1024UL*1024*1024);limit.jobMemory=new UIntPtr(2UL*1024*1024*1024);
            }
            Check(SetInformationJobObject(job,9,ref limit,Marshal.SizeOf(limit)));
            if(options!=null) {
                CPU_RATE cpu=new CPU_RATE();cpu.flags=5;cpu.rate=(uint)Math.Min(10000,20000/Math.Max(1,Environment.ProcessorCount));
                Check(SetCpuRate(job,15,ref cpu,Marshal.SizeOf(cpu)),"Job CPU limit");
            }
            STARTUPINFOEX startup=new STARTUPINFOEX(); startup.startup.cb=Marshal.SizeOf(startup); startup.attributes=attributes;
            if(options!=null) {
                SECURITY_ATTRIBUTES fileSecurity=new SECURITY_ATTRIBUTES();fileSecurity.length=Marshal.SizeOf(fileSecurity);fileSecurity.inherit=1;
                output=CreateFile(options.OutputPath,0xC0000000,1,ref fileSecurity,1,0x80,IntPtr.Zero);Check(output!=new IntPtr(-1),"Create owned capture file");
                input=CreateFile("NUL",0x80000000,3,ref fileSecurity,3,0x80,IntPtr.Zero);Check(input!=new IntPtr(-1),"Create owned null input");
                handles=Marshal.AllocHGlobal(IntPtr.Size*2);Marshal.WriteIntPtr(handles,0,input);Marshal.WriteIntPtr(handles,IntPtr.Size,output);
                Check(UpdateProcThreadAttribute(attributes,0,new IntPtr(0x20002),handles,new IntPtr(IntPtr.Size*2),IntPtr.Zero,IntPtr.Zero),"Explicit inherited stdio handles");
                startup.startup.flags=0x100;startup.startup.input=input;startup.startup.output=output;startup.startup.error=output;
            }
            envPtr=Marshal.StringToHGlobalUni(environment);
            Check(CreateProcess(executable,new StringBuilder(command),IntPtr.Zero,IntPtr.Zero,options!=null,0x08080404,envPtr,directory,ref startup,out process),"CreateProcess AppContainer");
            created=true;
            LastProcessId=process.pid;
            long exited,kernel,user;
            Check(GetProcessTimes(process.process,out LastCreationTime,out exited,out kernel,out user),"GetProcessTimes");
            Check(OpenProcessToken(process.process,8,out token));
            int isContainer,needed; Check(GetTokenInformation(token,29,out isContainer,4,out needed));
            if(isContainer!=1) throw new InvalidOperationException("Child token is not AppContainer");
            Check(AssignProcessToJobObject(job,process.process));
            if(progressPath!=null) {
                string temporary=progressPath+"."+Guid.NewGuid().ToString("N")+".tmp";
                File.WriteAllText(temporary,"{\"pid\":"+LastProcessId+",\"created\":"+LastCreationTime+"}");
                File.Move(temporary,progressPath);
            }
            var clock=System.Diagnostics.Stopwatch.StartNew();
            if(options!=null && options.GatePath!=null) {
                while(!File.Exists(options.GatePath)) {
                    if(options.CancelPath!=null && File.Exists(options.CancelPath)) throw new OperationCanceledException();
                    if(WaitForSingleObject(process.process,0)==0) throw new OperationCanceledException("Native job stopped before startup gate");
                    if(clock.ElapsedMilliseconds>=timeoutMs) throw new TimeoutException("Native startup gate timed out");
                    System.Threading.Thread.Sleep(10);
                }
            }
            if(options!=null && options.CancelPath!=null && File.Exists(options.CancelPath)) throw new OperationCanceledException();
            if(clock.ElapsedMilliseconds>=timeoutMs) throw new TimeoutException("Native startup deadline elapsed");
            if(ResumeThread(process.thread)==uint.MaxValue) throw new Win32Exception(Marshal.GetLastWin32Error());
            while(true) {
                uint wait=WaitForSingleObject(process.process,20);
                if(wait==0) break;
                if(wait!=258) throw new Win32Exception(Marshal.GetLastWin32Error());
                if(options!=null && options.CancelPath!=null && File.Exists(options.CancelPath)) throw new OperationCanceledException();
                if(clock.ElapsedMilliseconds>=timeoutMs) throw new TimeoutException("Isolated probe exceeded its finite timeout");
                if(output!=IntPtr.Zero) {long length;Check(GetFileSizeEx(output,out length),"Capture size");if(length>8388608) throw new OperationCanceledException("Native output limit exceeded");}
            }
            uint exit; Check(GetExitCodeProcess(process.process,out exit)); return (int)exit;
        } finally {
            try {
                // Stop all descendants before declaring completion or reading evidence.
                if(created) {
                    ACCOUNTING counts=Counts(job);
                    LastJobActiveBeforeStop=(int)counts.active; LastJobTotalProcesses=(int)counts.total;
                    Check(TerminateJobObject(job,125),"Stop complete job");
                    var stopClock=System.Diagnostics.Stopwatch.StartNew();
                    while(Counts(job).active!=0 && stopClock.ElapsedMilliseconds<5000) System.Threading.Thread.Sleep(10);
                    LastJobActiveAfterStop=(int)Counts(job).active;
                    if(LastJobActiveAfterStop!=0) throw new InvalidOperationException("Job stop unverified");
                    // Job assignment may have failed while the process was still suspended.
                    if(WaitForSingleObject(process.process,5000)!=0) {
                        // Termination may finish between the wait and TerminateProcess.
                        bool terminated=TerminateProcess(process.process,125);
                        if(WaitForSingleObject(process.process,5000)!=0)
                            throw new InvalidOperationException(terminated ? "Child stop unverified" : "Unbound child stop unverified");
                    }
                }
            } finally {
            if(token!=IntPtr.Zero) CloseHandle(token);
            if(job!=IntPtr.Zero) CloseHandle(job);
            if(process.thread!=IntPtr.Zero) CloseHandle(process.thread);
            if(process.process!=IntPtr.Zero) CloseHandle(process.process);
            if(initialized) DeleteProcThreadAttributeList(attributes);
            if(attributes!=IntPtr.Zero) Marshal.FreeHGlobal(attributes);
            if(capPtr!=IntPtr.Zero) Marshal.FreeHGlobal(capPtr);
            if(envPtr!=IntPtr.Zero) Marshal.FreeHGlobal(envPtr);
            if(input!=IntPtr.Zero && input!=new IntPtr(-1)) CloseHandle(input);
            if(output!=IntPtr.Zero && output!=new IntPtr(-1)) CloseHandle(output);
            if(handles!=IntPtr.Zero) Marshal.FreeHGlobal(handles);
            if(jobAttributes!=IntPtr.Zero) Marshal.FreeHGlobal(jobAttributes);
            if(jobDescriptor!=IntPtr.Zero) LocalFree(jobDescriptor);
            }
        }
    }
}
