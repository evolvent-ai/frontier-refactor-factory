/* A JVMTI agent that records which lines of one class actually executed.
 *
 * Java has no coverage in its standard library and JaCoCo is a third-party jar, which these tasks
 * cannot have: they build offline, with nothing but a JDK. What a JDK does ship is JVMTI and the
 * headers to write an agent against it, so this is that agent -- about eighty lines, compiled once
 * when the backend first needs it and cached beside it.
 *
 * HOW IT MEASURES. On ClassPrepare for the class under test it reads each method's line-number
 * table, which is the exact set of executable lines -- so the denominator is the JVM's own opinion
 * rather than a guess about which lines are code -- and sets a breakpoint at the first bytecode of
 * every line. A breakpoint that fires records the line and is CLEARED immediately, so a hot loop
 * pays for its first iteration only; leaving them armed would make an instrumented run hundreds of
 * times slower and would not record anything further.
 *
 * WHY NOT SINGLE-STEP. The obvious alternative, JVMTI_EVENT_SINGLE_STEP, reports every bytecode of
 * every thread and is slow enough to change what a large corpus can be measured at all.
 *
 * Written to a JSON file at VM death: {"executable":[...],"executed":[...]}. Both lists, because
 * the caller needs the denominator and not just the hits.
 *
 * Options are "<ClassName>,<output path>".
 */
#include <jvmti.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

static char g_class[256];
static char g_out[512];
static int  g_lines[4096]; static int g_nlines = 0;
static int  g_hit[4096];   static int g_nhit = 0;

static void add(int *arr, int *n, int v){
    for (int i=0;i<*n;i++) if (arr[i]==v) return;
    if (*n < 4096) arr[(*n)++] = v;
}

static void JNICALL ClassPrepare(jvmtiEnv *ti, JNIEnv *env, jthread t, jclass klass){
    char *sig=NULL;
    (void)env;(void)t;
    if ((*ti)->GetClassSignature(ti, klass, &sig, NULL)!=JVMTI_ERROR_NONE || !sig) return;
    if (!strstr(sig, g_class)) { (*ti)->Deallocate(ti,(unsigned char*)sig); return; }
    (*ti)->Deallocate(ti,(unsigned char*)sig);

    jint mcount=0; jmethodID *methods=NULL;
    if ((*ti)->GetClassMethods(ti, klass, &mcount, &methods)!=JVMTI_ERROR_NONE) return;
    for (jint i=0;i<mcount;i++){
        jint n=0; jvmtiLineNumberEntry *tbl=NULL;
        if ((*ti)->GetLineNumberTable(ti, methods[i], &n, &tbl)==JVMTI_ERROR_NONE){
            for (jint k=0;k<n;k++){
                add(g_lines,&g_nlines,tbl[k].line_number);
                (*ti)->SetBreakpoint(ti, methods[i], tbl[k].start_location);
            }
            (*ti)->Deallocate(ti,(unsigned char*)tbl);
        }
    }
    (*ti)->Deallocate(ti,(unsigned char*)methods);
}

static void JNICALL Breakpoint(jvmtiEnv *ti, JNIEnv *env, jthread t, jmethodID m, jlocation loc){
    jint n=0; jvmtiLineNumberEntry *tbl=NULL;
    (void)env;(void)t;
    if ((*ti)->GetLineNumberTable(ti, m, &n, &tbl)==JVMTI_ERROR_NONE){
        int line=-1;
        for (jint k=0;k<n;k++) if (tbl[k].start_location<=loc) line=tbl[k].line_number;
        if (line>0) add(g_hit,&g_nhit,line);
        (*ti)->Deallocate(ti,(unsigned char*)tbl);
    }
    (*ti)->ClearBreakpoint(ti, m, loc);
}

static void JNICALL VMDeath(jvmtiEnv *ti, JNIEnv *env){
    FILE *f; (void)ti;(void)env;
    f = fopen(g_out,"w"); if(!f) return;
    fprintf(f,"{\"executable\":[");
    for(int i=0;i<g_nlines;i++) fprintf(f,"%s%d", i?",":"", g_lines[i]);
    fprintf(f,"],\"executed\":[");
    for(int i=0;i<g_nhit;i++) fprintf(f,"%s%d", i?",":"", g_hit[i]);
    fprintf(f,"]}\n"); fclose(f);
}

JNIEXPORT jint JNICALL Agent_OnLoad(JavaVM *vm, char *options, void *reserved){
    jvmtiEnv *ti=NULL; jvmtiCapabilities caps; jvmtiEventCallbacks cb;
    char *comma;
    (void)reserved;
    if ((*vm)->GetEnv(vm,(void**)&ti,JVMTI_VERSION_1_2)!=JNI_OK) return JNI_ERR;
    snprintf(g_class,sizeof g_class,"%s", options?options:"Subject");
    comma=strchr(g_class,','); if(comma){ *comma=0; snprintf(g_out,sizeof g_out,"%s",comma+1); }
    else snprintf(g_out,sizeof g_out,"coverage.json");

    memset(&caps,0,sizeof caps);
    caps.can_generate_breakpoint_events=1;
    caps.can_get_line_numbers=1;
    if ((*ti)->AddCapabilities(ti,&caps)!=JVMTI_ERROR_NONE) return JNI_ERR;

    memset(&cb,0,sizeof cb);
    cb.ClassPrepare=&ClassPrepare; cb.Breakpoint=&Breakpoint; cb.VMDeath=&VMDeath;
    (*ti)->SetEventCallbacks(ti,&cb,sizeof cb);
    (*ti)->SetEventNotificationMode(ti,JVMTI_ENABLE,JVMTI_EVENT_CLASS_PREPARE,NULL);
    (*ti)->SetEventNotificationMode(ti,JVMTI_ENABLE,JVMTI_EVENT_BREAKPOINT,NULL);
    (*ti)->SetEventNotificationMode(ti,JVMTI_ENABLE,JVMTI_EVENT_VM_DEATH,NULL);
    return JNI_OK;
}
