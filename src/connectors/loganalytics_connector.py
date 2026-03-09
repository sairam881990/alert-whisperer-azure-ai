"""
Azure Log Analytics MCP Connector.

Connects to the Log Analytics MCP Server to query Spark driver/executor logs,
Synapse pipeline run history, and custom application logs. Provides structured
access to log data for failure detection and root cause analysis.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from src.connectors.mcp_base import BaseMCPClient
from src.models import AlertSource, DocumentChunk, PipelineType, RawLogEntry

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────
# Pre-built KQL Queries for Log Analytics
# ─────────────────────────────────────────────────

LA_QUERIES = {
    "spark_driver_errors": """
        SparkLoggingEvent_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Level_s in ("ERROR", "FATAL")
        | project TimeGenerated, Level_s, Message_s, LoggerName_s,
                  ClusterId_s, SparkJobId_s, ExceptionClass_s,
                  StackTrace_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    "spark_executor_failures": """
        SparkExecutorEvent_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where EventType_s == "ExecutorRemoved" or EventType_s == "ExecutorFailed"
        | project TimeGenerated, ExecutorId_s, Reason_s, ClusterId_s,
                  SparkJobId_s, EventType_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    "synapse_pipeline_failures": """
        SynapsePipelineRun_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Status_s == "Failed"
        | project TimeGenerated, PipelineName_s, RunId_s, ErrorMessage_s,
                  ActivityName_s, ActivityType_s, Duration_s,
                  ParameterJson_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    "synapse_activity_errors": """
        SynapseActivityRun_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Status_s == "Failed"
        | project TimeGenerated, PipelineName_s, ActivityName_s,
                  ActivityType_s, ErrorCode_s, ErrorMessage_s,
                  Input_s, Output_s, Duration_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    "oom_errors": """
        SparkLoggingEvent_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Message_s has "OutOfMemoryError" or Message_s has "OOM"
                or ExceptionClass_s has "OutOfMemoryError"
        | project TimeGenerated, Message_s, ClusterId_s, SparkJobId_s,
                  StackTrace_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    "timeout_errors": """
        union SparkLoggingEvent_CL, SynapsePipelineRun_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Message_s has "timeout" or Message_s has "Timeout"
                or ErrorMessage_s has "timeout"
        | project TimeGenerated, Message_s, ErrorMessage_s,
                  Type, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    "connection_errors": """
        union SparkLoggingEvent_CL, SynapsePipelineRun_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Message_s has_any ("ConnectionRefused", "ConnectionReset",
                "ConnectionTimeout", "SocketException", "UnknownHostException")
                or ErrorMessage_s has_any ("connection", "socket", "network")
        | project TimeGenerated, Message_s, ErrorMessage_s, Type
        | order by TimeGenerated desc
        | take {limit}
    """,

    "pipeline_run_history": """
        SynapsePipelineRun_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where PipelineName_s == "{pipeline_name}"
        | project TimeGenerated, Status_s, RunId_s, Duration_s,
                  ErrorMessage_s, ActivityName_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    "error_frequency": """
        union SparkLoggingEvent_CL, SynapsePipelineRun_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Level_s == "ERROR" or Status_s == "Failed"
        | summarize ErrorCount=count() by bin(TimeGenerated, 1h)
        | order by TimeGenerated desc
    """,

    "top_error_classes": """
        SparkLoggingEvent_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Level_s in ("ERROR", "FATAL")
        | summarize Count=count(), FirstSeen=min(TimeGenerated),
                    LastSeen=max(TimeGenerated),
                    SampleMessage=any(Message_s)
          by ExceptionClass_s
        | order by Count desc
        | take {limit}
    """,

    # ── D3: Standard Diagnostic Tables ───────────────────────────────────────

    # SparkListenerEvent_CL — stage/task granularity from Spark event listeners
    "spark_listener_events": """
        SparkListenerEvent_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Event_s in~ (
            "SparkListenerStageCompleted",
            "SparkListenerStageSubmitted",
            "SparkListenerTaskEnd",
            "SparkListenerJobEnd",
            "SparkListenerApplicationEnd"
          )
        | project TimeGenerated, Event_s, StageId_d, StageAttemptId_d,
                  JobId_d, TaskType_s, TaskEndReason_s, TaskFailureReason_s,
                  ExecutorId_s, ClusterId_s, SparkAppId_s,
                  NumTasks_d, NumFailedTasks_d, NumKilledTasks_d,
                  CompletionTime_d, SubmissionTime_d
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SparkLoggingEvent_CL — enhanced with job-level filtering support
    "spark_logging_by_job": """
        SparkLoggingEvent_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where isnotempty(SparkJobId_s)
        | where Level_s in ("ERROR", "FATAL", "WARN")
        | summarize
            ErrorCount   = countif(Level_s in ("ERROR", "FATAL")),
            WarnCount    = countif(Level_s == "WARN"),
            FirstSeen    = min(TimeGenerated),
            LastSeen     = max(TimeGenerated),
            SampleError  = anyif(Message_s, Level_s in ("ERROR", "FATAL")),
            SampleStack  = anyif(StackTrace_s, Level_s in ("ERROR", "FATAL")),
            ExceptionClasses = make_set(ExceptionClass_s, 20)
          by SparkJobId_s, ClusterId_s
        | order by LastSeen desc
        | take {limit}
    """,

    # SynapseBigDataPoolExternalEndpointEvent — pool connectivity events
    "synapse_pool_connectivity": """
        SynapseBigDataPoolExternalEndpointEvent
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | project TimeGenerated, OperationName, ResultType, ResultDescription,
                  PoolName_s, EndpointUrl_s, DurationMs,
                  _ResourceId, CorrelationId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SynapseBuiltinSqlPoolRequestsEnded — serverless SQL pool performance
    "synapse_serverless_sql": """
        SynapseBuiltinSqlPoolRequestsEnded
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | project TimeGenerated, RequestId, Command, LoginName,
                  DurationMs, DataProcessedBytes, Status,
                  ErrorCode, SqlHandle, _ResourceId
        | extend DataProcessedGB = round(DataProcessedBytes / 1073741824.0, 3)
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SynapseBuiltinSqlPoolRequestsEnded — failed serverless SQL requests
    "synapse_serverless_sql_errors": """
        SynapseBuiltinSqlPoolRequestsEnded
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Status != "Succeeded"
        | project TimeGenerated, RequestId, Command, LoginName,
                  DurationMs, DataProcessedBytes, Status,
                  ErrorCode, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SynapseIntegrationActivityRuns — ADF/Synapse IR activity-level runs
    "synapse_integration_activity_runs": """
        SynapseIntegrationActivityRuns
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | project TimeGenerated, PipelineName, ActivityName, ActivityType,
                  Status, Start, End, DurationInMs,
                  ErrorCode, ErrorMessage, EffectiveIntegrationRuntime,
                  BilledDuration, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SynapseIntegrationActivityRuns — failed only
    "synapse_integration_activity_failures": """
        SynapseIntegrationActivityRuns
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Status == "Failed"
        | project TimeGenerated, PipelineName, ActivityName, ActivityType,
                  DurationInMs, ErrorCode, ErrorMessage,
                  EffectiveIntegrationRuntime, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SynapseIntegrationPipelineRuns — pipeline-level runs
    "synapse_integration_pipeline_runs": """
        SynapseIntegrationPipelineRuns
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | project TimeGenerated, PipelineName, RunId, Status,
                  Start, End, DurationInMs,
                  ErrorCode, Message, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SynapseIntegrationPipelineRuns — failures only
    "synapse_integration_pipeline_failures": """
        SynapseIntegrationPipelineRuns
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Status == "Failed"
        | project TimeGenerated, PipelineName, RunId,
                  DurationInMs, ErrorCode, Message, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SynapseIntegrationTriggerRuns — trigger executions
    "synapse_trigger_runs": """
        SynapseIntegrationTriggerRuns
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | project TimeGenerated, TriggerName, TriggerType, Status,
                  TriggeredPipelines, Message, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SynapseSqlPoolDmsWorkers — DMS worker events for dedicated SQL pools
    "synapse_dms_workers": """
        SynapseSqlPoolDmsWorkers
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | project TimeGenerated, RequestId, StepIndex, DmsStepIndex,
                  Status, Type, StartTime, EndTime,
                  TotalElapsedTime, CpuTime, ReadsCount, ReadSizeBytes,
                  RowsProcessed, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SynapseSqlPoolExecRequests — execution requests (dedicated pool)
    "synapse_exec_requests": """
        SynapseSqlPoolExecRequests
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | project TimeGenerated, RequestId, SessionId, Status,
                  Command, SubmitTime, StartTime, EndTime,
                  TotalElapsedTimeMs, CpuTime, LogicalReads,
                  Reads, Writes, RowCount,
                  WaitType, BlockingSessionId, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SynapseSqlPoolExecRequests — long-running or failed requests
    "synapse_exec_requests_slow": """
        SynapseSqlPoolExecRequests
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Status in ("Failed", "Cancelled") or TotalElapsedTimeMs > 300000
        | project TimeGenerated, RequestId, SessionId, Status,
                  Command, TotalElapsedTimeMs, CpuTime,
                  LogicalReads, WaitType, BlockingSessionId, _ResourceId
        | extend ElapsedSec = round(TotalElapsedTimeMs / 1000.0, 1)
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SynapseSqlPoolRequestSteps — request step details
    "synapse_request_steps": """
        SynapseSqlPoolRequestSteps
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | project TimeGenerated, RequestId, StepIndex, OperationType,
                  Status, StartTime, EndTime,
                  TotalElapsedTimeMs, EstimatedRowsCount,
                  ActualRowsCount, Command, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SynapseSqlPoolSqlRequests — SQL-level requests (dedicated pool)
    "synapse_sql_requests": """
        SynapseSqlPoolSqlRequests
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | project TimeGenerated, RequestId, SessionId, Status,
                  DistributionId, Command,
                  StartTime, EndTime, TotalElapsedTimeMs,
                  LogicalReads, Reads, Writes, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SynapseSqlPoolWaits — wait statistics (dedicated pool)
    "synapse_sql_pool_waits": """
        SynapseSqlPoolWaits
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | project TimeGenerated, RequestId, SessionId, Type,
                  ObjectType, ObjectName, State,
                  WaitTime, BlockingSessionId, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # SynapseSqlPoolWaits — aggregated wait analysis
    "synapse_wait_analysis": """
        SynapseSqlPoolWaits
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | summarize
            WaitCount      = count(),
            TotalWaitMs    = sum(WaitTime),
            AvgWaitMs      = avg(WaitTime),
            MaxWaitMs      = max(WaitTime),
            AffectedSessions = dcount(SessionId)
          by Type
        | order by TotalWaitMs desc
        | take {limit}
    """,

    # AzureDiagnostics — generic Azure diagnostic events, filtered to Synapse/Spark
    "azure_diagnostics_synapse": """
        AzureDiagnostics
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where ResourceProvider in~ (
            "MICROSOFT.SYNAPSE",
            "MICROSOFT.DATAFACTORY",
            "MICROSOFT.HDINSIGHT"
          )
        | project TimeGenerated, ResourceProvider, ResourceType,
                  OperationName, ResultType, ResultDescription,
                  Level, Category, DurationMs,
                  ErrorCode_s, ErrorMessage_s,
                  Resource, ResourceGroup, SubscriptionId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # AzureDiagnostics — failures only
    "azure_diagnostics_failures": """
        AzureDiagnostics
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where ResourceProvider in~ (
            "MICROSOFT.SYNAPSE",
            "MICROSOFT.DATAFACTORY",
            "MICROSOFT.HDINSIGHT"
          )
        | where ResultType in~ ("Failed", "Error", "Failure")
              or Level in~ ("Error", "Critical")
        | project TimeGenerated, ResourceProvider, ResourceType,
                  OperationName, ResultType, ResultDescription,
                  Category, DurationMs, ErrorCode_s, ErrorMessage_s,
                  Resource, ResourceGroup
        | order by TimeGenerated desc
        | take {limit}
    """,

    # ADFPipelineRun — ADF pipeline runs
    "adf_pipeline_runs": """
        ADFPipelineRun
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | project TimeGenerated, PipelineName, RunId, Status,
                  Start, End, DurationInMs,
                  ErrorCode, Message, Annotations, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # ADFPipelineRun — failures only
    "adf_pipeline_failures": """
        ADFPipelineRun
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Status == "Failed"
        | project TimeGenerated, PipelineName, RunId,
                  DurationInMs, ErrorCode, Message, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # ADFActivityRun — ADF activity runs
    "adf_activity_runs": """
        ADFActivityRun
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | project TimeGenerated, PipelineName, ActivityName, ActivityType,
                  Status, Start, End, DurationInMs,
                  ErrorCode, ErrorMessage,
                  EffectiveIntegrationRuntime, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # ADFActivityRun — failures only
    "adf_activity_failures": """
        ADFActivityRun
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Status == "Failed"
        | project TimeGenerated, PipelineName, ActivityName, ActivityType,
                  DurationInMs, ErrorCode, ErrorMessage,
                  EffectiveIntegrationRuntime, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # ADFTriggerRun — ADF trigger executions
    "adf_trigger_runs": """
        ADFTriggerRun
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | project TimeGenerated, TriggerName, TriggerType, Status,
                  TriggeredPipelines, Message, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    # ── D4: Executor-Level Metrics ────────────────────────────────────────────

    # Executor memory, GC time, shuffle read/write — per-executor aggregation
    "spark_executor_metrics": """
        SparkExecutorEvent_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where EventType_s in (
            "ExecutorMetricsUpdate",
            "ExecutorAdded",
            "ExecutorRemoved",
            "ExecutorFailed"
          )
        | summarize
            AvgMemUsedBytes      = avg(todouble(MemoryUsed_d)),
            MaxMemUsedBytes      = max(todouble(MemoryUsed_d)),
            AvgGCTimeMs          = avg(todouble(TotalGCTime_d)),
            MaxGCTimeMs          = max(todouble(TotalGCTime_d)),
            TotalShuffleReadBytes  = sum(todouble(ShuffleRead_d)),
            TotalShuffleWriteBytes = sum(todouble(ShuffleWrite_d)),
            TotalDiskSpillBytes  = sum(todouble(DiskBytesSpilled_d)),
            TotalMemorySpillBytes= sum(todouble(MemoryBytesSpilled_d)),
            ExecutorLostCount    = countif(EventType_s in ("ExecutorRemoved", "ExecutorFailed")),
            LastSeen             = max(TimeGenerated)
          by ExecutorId_s, ClusterId_s, SparkJobId_s
        | extend
            AvgMemUsedGB   = round(AvgMemUsedBytes / 1073741824.0, 3),
            MaxMemUsedGB   = round(MaxMemUsedBytes / 1073741824.0, 3),
            ShuffleReadGB  = round(TotalShuffleReadBytes / 1073741824.0, 3),
            ShuffleWriteGB = round(TotalShuffleWriteBytes / 1073741824.0, 3)
        | order by LastSeen desc
        | take {limit}
    """,

    # Executor GC pressure — flag executors spending >20% of time in GC
    "spark_executor_gc_pressure": """
        SparkExecutorEvent_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where EventType_s == "ExecutorMetricsUpdate"
        | where isnotnull(TotalGCTime_d) and isnotnull(TotalDuration_d)
        | extend GCPct = iff(
            todouble(TotalDuration_d) > 0,
            100.0 * todouble(TotalGCTime_d) / todouble(TotalDuration_d),
            0.0
          )
        | where GCPct > 20.0
        | project TimeGenerated, ExecutorId_s, ClusterId_s, SparkJobId_s,
                  GCPct, TotalGCTime_d, TotalDuration_d,
                  MemoryUsed_d, MaxMemory_d
        | order by TimeGenerated desc
        | take {limit}
    """,

    # ── D5: Spark Stage / Task-Level Performance ──────────────────────────────

    # Stage completion times and failure rates
    "spark_stage_performance": """
        SparkListenerEvent_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Event_s == "SparkListenerStageCompleted"
        | extend
            StageDurationMs  = todouble(CompletionTime_d) - todouble(SubmissionTime_d),
            FailureRate      = iff(NumTasks_d > 0,
                                  100.0 * todouble(NumFailedTasks_d) / todouble(NumTasks_d),
                                  0.0)
        | project TimeGenerated, StageId_d, StageAttemptId_d, JobId_d,
                  ClusterId_s, SparkAppId_s,
                  StageDurationMs,
                  NumTasks_d, NumFailedTasks_d, NumKilledTasks_d,
                  FailureRate,
                  ShuffleReadBytes_d, ShuffleWriteBytes_d,
                  InputBytes_d, OutputBytes_d,
                  MemoryBytesSpilled_d, DiskBytesSpilled_d,
                  FailureReason_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    # Slowest stages — ordered by duration descending
    "spark_slow_stages": """
        SparkListenerEvent_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Event_s == "SparkListenerStageCompleted"
        | extend StageDurationMs = todouble(CompletionTime_d) - todouble(SubmissionTime_d)
        | where StageDurationMs > 60000  // stages taking > 1 min
        | project TimeGenerated, StageId_d, StageAttemptId_d, JobId_d,
                  ClusterId_s, SparkAppId_s,
                  StageDurationMs,
                  NumTasks_d, NumFailedTasks_d,
                  ShuffleReadBytes_d, ShuffleWriteBytes_d,
                  InputBytes_d, OutputBytes_d,
                  MemoryBytesSpilled_d, DiskBytesSpilled_d
        | order by StageDurationMs desc
        | take {limit}
    """,

    # Task-level failures
    "spark_task_failures": """
        SparkListenerEvent_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Event_s == "SparkListenerTaskEnd"
        | where TaskEndReason_s !in~ ("Success", "TaskKilled")
        | project TimeGenerated, StageId_d, StageAttemptId_d, JobId_d,
                  TaskType_s, TaskEndReason_s, TaskFailureReason_s,
                  ExecutorId_s, ClusterId_s, SparkAppId_s,
                  Duration_d, GCTime_d,
                  ShuffleReadBytes_d, ShuffleWriteBytes_d,
                  MemoryBytesSpilled_d, DiskBytesSpilled_d
        | order by TimeGenerated desc
        | take {limit}
    """,

    # Stage shuffle volume analysis — high-shuffle stages indicate skew
    "spark_stage_shuffle_analysis": """
        SparkListenerEvent_CL
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Event_s == "SparkListenerStageCompleted"
        | extend
            ShuffleReadGB  = round(todouble(ShuffleReadBytes_d)  / 1073741824.0, 3),
            ShuffleWriteGB = round(todouble(ShuffleWriteBytes_d) / 1073741824.0, 3),
            SpillGB        = round(todouble(MemoryBytesSpilled_d) / 1073741824.0, 3)
        | where ShuffleReadGB > 1.0 or ShuffleWriteGB > 1.0 or SpillGB > 0.5
        | project TimeGenerated, StageId_d, JobId_d, ClusterId_s,
                  ShuffleReadGB, ShuffleWriteGB, SpillGB,
                  NumTasks_d, NumFailedTasks_d
        | order by TimeGenerated desc
        | take {limit}
    """,

    # ── D6: Synapse Serverless SQL Pool Performance ───────────────────────────
    # (Additional focused queries beyond the base synapse_serverless_sql above)

    # Top slowest serverless SQL queries
    "synapse_serverless_slow_queries": """
        SynapseBuiltinSqlPoolRequestsEnded
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where Status == "Succeeded"
        | where DurationMs > 30000  // queries taking > 30 seconds
        | extend DataProcessedGB = round(DataProcessedBytes / 1073741824.0, 3)
        | project TimeGenerated, RequestId, LoginName,
                  DurationMs, DataProcessedGB, Status,
                  Command, SqlHandle, _ResourceId
        | order by DurationMs desc
        | take {limit}
    """,

    # Serverless SQL throughput summary — data processed per hour
    "synapse_serverless_throughput": """
        SynapseBuiltinSqlPoolRequestsEnded
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | summarize
            QueryCount        = count(),
            FailedQueries     = countif(Status != "Succeeded"),
            AvgDurationMs     = avg(DurationMs),
            P95DurationMs     = percentile(DurationMs, 95),
            TotalDataGB       = round(sum(DataProcessedBytes) / 1073741824.0, 3)
          by bin(TimeGenerated, 1h)
        | order by TimeGenerated desc
        | take {limit}
    """,

    # ── D7: ADF Integration Runtime Health ───────────────────────────────────

    # IR node health and activity
    "adf_ir_status": """
        AzureDiagnostics
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where ResourceProvider =~ "MICROSOFT.DATAFACTORY"
        | where Category =~ "IntegrationRuntimeAvailabilityResults"
              or OperationName has_any ("IntegrationRuntime", "IR")
        | project TimeGenerated, OperationName, ResultType, ResultDescription,
                  Level, Resource, ResourceGroup,
                  RunId_s, IRName_s, NodeName_s, ErrorCode_s, Message_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    # ADF IR SHIR (Self-Hosted IR) node issues
    "adf_shir_issues": """
        AzureDiagnostics
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where ResourceProvider =~ "MICROSOFT.DATAFACTORY"
        | where Category =~ "SelfHostedIntegrationRuntimeAvailabilityResults"
              or OperationName has_any ("SelfHosted", "SHIR")
        | where ResultType in~ ("Failed", "Unhealthy", "Offline")
              or Level =~ "Error"
        | project TimeGenerated, OperationName, ResultType, ResultDescription,
                  Level, Resource, ResourceGroup,
                  ErrorCode_s, Message_s, NodeName_s
        | order by TimeGenerated desc
        | take {limit}
    """,

    # ADF Copy activity throughput — data movement efficiency
    "adf_copy_throughput": """
        ADFActivityRun
        | where TimeGenerated > ago({timespan})
        {since_filter}
        | where ActivityType == "Copy"
        | extend
            ThroughputMBps = round(
                todouble(Output.dataRead) / 1048576.0
                / iff(DurationInMs > 0, todouble(DurationInMs) / 1000.0, 1.0),
                2
            ),
            ReadGB  = round(todouble(Output.dataRead)    / 1073741824.0, 3),
            WriteGB = round(todouble(Output.dataWritten) / 1073741824.0, 3)
        | project TimeGenerated, PipelineName, ActivityName, Status,
                  DurationInMs, ThroughputMBps, ReadGB, WriteGB,
                  EffectiveIntegrationRuntime, _ResourceId
        | order by TimeGenerated desc
        | take {limit}
    """,

    "custom_query": "{query}",
}


class LogAnalyticsMCPClient(BaseMCPClient):
    """
    MCP client for Azure Log Analytics.

    Provides methods to:
    - Query Spark driver and executor logs
    - Retrieve Synapse pipeline failures
    - Detect specific error patterns (OOM, timeout, connection)
    - Analyze error frequencies and trends
    - Index log data for RAG knowledge base
    """

    def __init__(
        self,
        server_url: str,
        workspace_id: str = "",
        default_timespan: str = "P1D",
        **kwargs: Any,
    ):
        super().__init__(server_url=server_url, **kwargs)
        self.workspace_id = workspace_id
        self.default_timespan = default_timespan

    async def validate_connection(self) -> bool:
        """Validate connectivity to Log Analytics."""
        try:
            result = await self.execute_query("print 'ok'", timespan="PT5M")
            return result is not None
        except Exception as e:
            logger.error("log_analytics_validation_failed", error=str(e))
            return False

    async def execute_query(
        self,
        query: str,
        timespan: Optional[str] = None,
    ) -> Any:
        """
        Execute a KQL query against Log Analytics workspace.

        Args:
            query: KQL query string
            timespan: ISO 8601 duration (e.g., 'P1D' for 1 day)

        Returns:
            Query results from MCP server
        """
        ts = timespan or self.default_timespan
        logger.info("la_query", query_preview=query[:120], timespan=ts)

        return await self.call_tool(
            tool_name="query_logs",
            arguments={
                "query": query.strip(),
                "workspace_id": self.workspace_id,
                "timespan": ts,
            },
        )

    # ── D8: Incremental / since-based query helper ────────────────────────────

    @staticmethod
    def _build_since_filter(since_timestamp: Optional[datetime]) -> str:
        """
        Build a KQL `| where TimeGenerated > datetime(...)` fragment for
        incremental data loads.  Returns an empty string when no cursor is given,
        so callers can safely embed `{since_filter}` in every query template
        without conditional logic.

        The returned fragment already contains the leading pipe, e.g.:
            '| where TimeGenerated > datetime(2026-03-08T12:00:00Z)'
        """
        if since_timestamp is None:
            return ""  # no-op — first full load
        iso = since_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"| where TimeGenerated > datetime({iso})"

    async def execute_query_incremental(
        self,
        query_key: str,
        since_timestamp: Optional[datetime] = None,
        timespan: Optional[str] = None,
        limit: int = 10000,
        **extra_params: Any,
    ) -> Any:
        """
        Execute a named query from LA_QUERIES with optional incremental cursor.

        Supports D8 by injecting a `since_timestamp` filter on top of the
        existing timespan window.  When `since_timestamp` is provided, only
        rows newer than that cursor are returned — ideal for refresh cycles
        where we don't want to re-ingest already-seen events.

        Args:
            query_key:       Key in LA_QUERIES dict.
            since_timestamp: Optional datetime cursor for incremental loads.
            timespan:        ISO 8601 window (default: self.default_timespan).
            limit:           Maximum rows returned.
            **extra_params:  Additional format parameters for the query template.

        Returns:
            Raw query results from MCP server.
        """
        template = LA_QUERIES[query_key]
        since_filter = self._build_since_filter(since_timestamp)
        ts = timespan or self.default_timespan

        query = template.format(
            timespan=ts,
            limit=limit,
            since_filter=since_filter,
            **extra_params,
        )
        logger.info(
            "la_incremental_query",
            query_key=query_key,
            since=since_timestamp.isoformat() if since_timestamp else "full",
            timespan=ts,
            limit=limit,
        )
        return await self.execute_query(query, timespan=ts)

    # ── D10: Pre-indexing volume estimation ───────────────────────────────────

    async def count_query_results(
        self,
        query_key: str,
        timespan: Optional[str] = None,
        since_timestamp: Optional[datetime] = None,
        **extra_params: Any,
    ) -> int:
        """
        Estimate the row count for a query *without* fetching all data.

        Wraps the named LA_QUERIES template in a KQL `count` aggregation,
        stripping the `take/limit` clause so we get the true row count.
        This prevents overwhelming the indexing pipeline with unexpectedly
        large result sets (D10).

        Args:
            query_key:       Key in LA_QUERIES dict (must support {timespan},
                             {since_filter}, {limit} placeholders).
            timespan:        ISO 8601 window.
            since_timestamp: Optional incremental cursor.
            **extra_params:  Additional format parameters.

        Returns:
            Estimated row count (int). Returns -1 on error so callers can
            choose to proceed with a warning rather than hard-fail.
        """
        ts = timespan or self.default_timespan
        since_filter = self._build_since_filter(since_timestamp)

        # Render the base query with a very high limit so the count reflects
        # the true volume, then wrap in a KQL count summarize.
        try:
            base_query = LA_QUERIES[query_key].format(
                timespan=ts,
                limit=500000,  # effectively uncapped for estimation
                since_filter=since_filter,
                **extra_params,
            )
        except KeyError as exc:
            logger.warning("count_query_format_error", query_key=query_key, error=str(exc))
            return -1

        # Strip trailing `| take N` line from the base query and wrap with count
        import re
        base_clean = re.sub(r"\|\s*take\s+\d+\s*$", "", base_query.strip(), flags=re.IGNORECASE)
        count_query = f"{base_clean}\n        | count"

        logger.info(
            "la_count_query",
            query_key=query_key,
            timespan=ts,
            since=since_timestamp.isoformat() if since_timestamp else "full",
        )

        try:
            raw = await self.execute_query(count_query, timespan=ts)
            rows = self._normalize_rows(raw)
            if rows:
                # count returns a single row with column "Count"
                first = rows[0]
                count_val = first.get("Count") or first.get("count") or 0
                return int(count_val)
            return 0
        except Exception as e:
            logger.warning("count_query_failed", query_key=query_key, error=str(e))
            return -1

    async def estimate_and_warn(
        self,
        query_key: str,
        warn_threshold: int = 50000,
        hard_limit: int = 500000,
        timespan: Optional[str] = None,
        since_timestamp: Optional[datetime] = None,
        **extra_params: Any,
    ) -> tuple[int, bool]:
        """
        Run a count estimate and emit structured log warnings if thresholds
        are exceeded.  Returns ``(estimated_count, exceeded_hard_limit)``.

        Used in ``get_logs_for_indexing`` before fetching full result sets.
        """
        count = await self.count_query_results(
            query_key=query_key,
            timespan=timespan,
            since_timestamp=since_timestamp,
            **extra_params,
        )
        exceeded = False
        if count < 0:
            logger.warning("volume_estimate_unavailable", query_key=query_key)
        elif count > hard_limit:
            logger.warning(
                "volume_exceeds_hard_limit",
                query_key=query_key,
                estimated_rows=count,
                hard_limit=hard_limit,
            )
            exceeded = True
        elif count > warn_threshold:
            logger.warning(
                "volume_exceeds_warn_threshold",
                query_key=query_key,
                estimated_rows=count,
                warn_threshold=warn_threshold,
            )
        else:
            logger.info("volume_estimate_ok", query_key=query_key, estimated_rows=count)
        return count, exceeded

    async def get_spark_driver_errors(
        self,
        timespan: str = "24h",
        limit: int = 50,
        since_timestamp: Optional[datetime] = None,
    ) -> list[RawLogEntry]:
        """Retrieve Spark driver-level errors."""
        query = LA_QUERIES["spark_driver_errors"].format(
            timespan=timespan,
            limit=limit,
            since_filter=self._build_since_filter(since_timestamp),
        )
        raw = await self.execute_query(query)
        return self._parse_spark_logs(raw)

    async def get_spark_executor_failures(
        self,
        timespan: str = "24h",
        limit: int = 50,
        since_timestamp: Optional[datetime] = None,
    ) -> list[RawLogEntry]:
        """Retrieve Spark executor removal/failure events."""
        query = LA_QUERIES["spark_executor_failures"].format(
            timespan=timespan,
            limit=limit,
            since_filter=self._build_since_filter(since_timestamp),
        )
        raw = await self.execute_query(query)
        return self._parse_spark_executor_events(raw)

    async def get_synapse_pipeline_failures(
        self,
        timespan: str = "24h",
        limit: int = 50,
        since_timestamp: Optional[datetime] = None,
    ) -> list[RawLogEntry]:
        """Retrieve Synapse pipeline run failures."""
        query = LA_QUERIES["synapse_pipeline_failures"].format(
            timespan=timespan,
            limit=limit,
            since_filter=self._build_since_filter(since_timestamp),
        )
        raw = await self.execute_query(query)
        return self._parse_synapse_failures(raw)

    async def get_synapse_activity_errors(
        self,
        timespan: str = "24h",
        limit: int = 50,
        since_timestamp: Optional[datetime] = None,
    ) -> list[RawLogEntry]:
        """Retrieve Synapse activity-level errors."""
        query = LA_QUERIES["synapse_activity_errors"].format(
            timespan=timespan,
            limit=limit,
            since_filter=self._build_since_filter(since_timestamp),
        )
        raw = await self.execute_query(query)
        return self._parse_synapse_activities(raw)

    async def detect_oom_errors(
        self,
        timespan: str = "24h",
        limit: int = 20,
        since_timestamp: Optional[datetime] = None,
    ) -> list[RawLogEntry]:
        """Detect OutOfMemoryError occurrences."""
        query = LA_QUERIES["oom_errors"].format(
            timespan=timespan,
            limit=limit,
            since_filter=self._build_since_filter(since_timestamp),
        )
        raw = await self.execute_query(query)
        return self._parse_spark_logs(raw)

    async def detect_timeout_errors(
        self,
        timespan: str = "24h",
        limit: int = 20,
        since_timestamp: Optional[datetime] = None,
    ) -> list[RawLogEntry]:
        """Detect timeout-related errors."""
        query = LA_QUERIES["timeout_errors"].format(
            timespan=timespan,
            limit=limit,
            since_filter=self._build_since_filter(since_timestamp),
        )
        raw = await self.execute_query(query)
        return self._parse_generic_logs(raw, "timeout")

    async def detect_connection_errors(
        self,
        timespan: str = "24h",
        limit: int = 20,
        since_timestamp: Optional[datetime] = None,
    ) -> list[RawLogEntry]:
        """Detect connection/network related errors."""
        query = LA_QUERIES["connection_errors"].format(
            timespan=timespan,
            limit=limit,
            since_filter=self._build_since_filter(since_timestamp),
        )
        raw = await self.execute_query(query)
        return self._parse_generic_logs(raw, "connection")

    async def get_pipeline_history(
        self,
        pipeline_name: str,
        timespan: str = "7d",
        limit: int = 20,
        since_timestamp: Optional[datetime] = None,
    ) -> Any:
        """Get run history for a specific pipeline."""
        query = LA_QUERIES["pipeline_run_history"].format(
            pipeline_name=pipeline_name,
            timespan=timespan,
            limit=limit,
            since_filter=self._build_since_filter(since_timestamp),
        )
        return await self.execute_query(query)

    async def get_error_frequency(
        self,
        timespan: str = "24h",
        since_timestamp: Optional[datetime] = None,
    ) -> Any:
        """Get hourly error count trend."""
        query = LA_QUERIES["error_frequency"].format(
            timespan=timespan,
            since_filter=self._build_since_filter(since_timestamp),
        )
        return await self.execute_query(query)

    async def get_top_error_classes(
        self,
        timespan: str = "7d",
        limit: int = 10,
        since_timestamp: Optional[datetime] = None,
    ) -> Any:
        """Get most frequent error classes."""
        query = LA_QUERIES["top_error_classes"].format(
            timespan=timespan,
            limit=limit,
            since_filter=self._build_since_filter(since_timestamp),
        )
        return await self.execute_query(query)

    async def run_custom_query(self, query: str) -> Any:
        """Run a custom KQL query provided by the user."""
        return await self.execute_query(query)

    # ─── M4: Per-Connector Health Check ───────────────────

    async def health_check(self) -> dict[str, Any]:
        """
        M4: Log Analytics-specific health check.
        Pings the MCP /health endpoint and runs a trivial KQL query
        to verify the underlying Log Analytics workspace is reachable.
        """
        status = await super().health_check()
        status["connector"] = "log_analytics"
        status["workspace_id"] = self.workspace_id

        if status["healthy"]:
            try:
                result = await self.execute_query("print 'ok'", timespan="PT5M")
                status["workspace_reachable"] = result is not None
                status["workspace_detail"] = "KQL probe query succeeded"
            except Exception as exc:
                status["workspace_reachable"] = False
                status["workspace_detail"] = f"{type(exc).__name__}: {exc}"

        return status

    async def get_logs_for_indexing(
        self,
        timespan: str = "30d",
        batch_size: int = 10000,
        max_batches: int = 10,
        since_timestamp: Optional[datetime] = None,
        warn_threshold: int = 50000,
        hard_limit: int = 500000,
    ) -> list[DocumentChunk]:
        """
        Retrieve error logs for RAG indexing with pagination and volume guards.

        D1: Default `batch_size` is 10000 (up from 200) and supports multi-batch
        pagination using a cursor pattern.  Each batch uses `since_timestamp` to
        advance the cursor so no records are double-counted.

        D8: Pass `since_timestamp` to fetch only new records since the last index
        cycle, making this suitable for incremental refresh loops.

        D10: Runs a count estimation before fetching.  If the estimated row count
        exceeds `hard_limit`, the fetch is aborted with a warning.  If it exceeds
        `warn_threshold`, a warning is logged but the fetch proceeds.

        Args:
            timespan:        ISO 8601 lookback window (default 30d).
            batch_size:      Records per batch (default 10000).  Overridden by
                             LOG_ANALYTICS_INDEXING_BATCH_SIZE env var when using
                             LogAnalyticsSettings.
            max_batches:     Maximum pagination rounds (default 10).
            since_timestamp: Incremental cursor — only fetch records newer than
                             this datetime.  None means full re-index.
            warn_threshold:  Warn if estimated row count exceeds this.
            hard_limit:      Abort if estimated row count exceeds this.

        Returns:
            List of DocumentChunk objects for vector store ingestion.
        """
        # ── D10: volume pre-check ──
        for query_key in ("spark_driver_errors", "synapse_pipeline_failures"):
            _, exceeded = await self.estimate_and_warn(
                query_key=query_key,
                warn_threshold=warn_threshold,
                hard_limit=hard_limit,
                timespan=timespan,
                since_timestamp=since_timestamp,
            )
            if exceeded:
                logger.error(
                    "indexing_aborted_volume_exceeded",
                    query_key=query_key,
                    hard_limit=hard_limit,
                )
                return []

        # ── D1 + D8: paginated, incremental fetch ──
        all_logs: list[RawLogEntry] = []
        cursor: Optional[datetime] = since_timestamp

        for batch_num in range(1, max_batches + 1):
            spark_batch = await self.get_spark_driver_errors(
                timespan=timespan,
                limit=batch_size,
                since_timestamp=cursor,
            )
            synapse_batch = await self.get_synapse_pipeline_failures(
                timespan=timespan,
                limit=batch_size,
                since_timestamp=cursor,
            )
            batch = spark_batch + synapse_batch

            if not batch:
                logger.info(
                    "la_indexing_batch_empty",
                    batch_num=batch_num,
                    total_so_far=len(all_logs),
                )
                break

            all_logs.extend(batch)
            logger.info(
                "la_indexing_batch_fetched",
                batch_num=batch_num,
                batch_size=len(batch),
                total_so_far=len(all_logs),
            )

            # Advance cursor to the latest timestamp in this batch so the
            # next batch only fetches strictly newer records (D8 pagination).
            if batch:
                latest_ts = max(log.timestamp for log in batch)
                cursor = latest_ts

            # If both sources returned fewer rows than batch_size, no more pages.
            if len(spark_batch) < batch_size and len(synapse_batch) < batch_size:
                break

        # ── Convert to DocumentChunks ──
        chunks: list[DocumentChunk] = []
        for log in all_logs:
            content = (
                f"Source: {log.source.value}\n"
                f"Level: {log.level}\n"
                f"Pipeline: {log.pipeline_name or 'unknown'}\n"
                f"Message: {log.message}\n"
                f"Job ID: {log.job_id or 'N/A'}\n"
                f"Metadata: {log.metadata}"
            )

            chunks.append(
                DocumentChunk(
                    chunk_id=f"log_{log.source.value}_{log.timestamp.isoformat()}",
                    source_type="log_analytics",
                    source_id=log.job_id or log.timestamp.isoformat(),
                    source_title=f"{log.source.value} error - {log.pipeline_name or 'unknown'}",
                    content=content,
                    metadata={
                        "source": log.source.value,
                        "level": log.level,
                        "pipeline": log.pipeline_name,
                        "timestamp": log.timestamp.isoformat(),
                    },
                )
            )

        logger.info(
            "la_indexed",
            total_chunks=len(chunks),
            batches_fetched=batch_num if all_logs else 0,
            since=since_timestamp.isoformat() if since_timestamp else "full",
        )
        return chunks

    # ─── Internal Parsers ─────────────────────────

    def _parse_spark_logs(self, raw: Any) -> list[RawLogEntry]:
        """Parse Spark log entries from Log Analytics results."""
        entries = []
        rows = self._normalize_rows(raw)

        for row in rows:
            try:
                entries.append(
                    RawLogEntry(
                        timestamp=self._parse_timestamp(row.get("TimeGenerated")),
                        source=AlertSource.LOG_ANALYTICS,
                        level=row.get("Level_s", "ERROR"),
                        message=row.get("Message_s", ""),
                        metadata={
                            "exception_class": row.get("ExceptionClass_s", ""),
                            "logger": row.get("LoggerName_s", ""),
                            "stack_trace": str(row.get("StackTrace_s", ""))[:2000],
                        },
                        pipeline_name=f"spark_{row.get('ClusterId_s', 'unknown')}",
                        job_id=row.get("SparkJobId_s"),
                        cluster_id=row.get("ClusterId_s"),
                    )
                )
            except Exception as e:
                logger.warning("spark_log_parse_error", error=str(e))

        return entries

    def _parse_spark_executor_events(self, raw: Any) -> list[RawLogEntry]:
        """Parse Spark executor failure events."""
        entries = []
        rows = self._normalize_rows(raw)

        for row in rows:
            try:
                entries.append(
                    RawLogEntry(
                        timestamp=self._parse_timestamp(row.get("TimeGenerated")),
                        source=AlertSource.LOG_ANALYTICS,
                        level="ERROR",
                        message=f"Executor {row.get('ExecutorId_s', '?')} {row.get('EventType_s', 'failed')}: "
                                f"{row.get('Reason_s', 'Unknown reason')}",
                        metadata={
                            "executor_id": row.get("ExecutorId_s", ""),
                            "event_type": row.get("EventType_s", ""),
                        },
                        pipeline_name=f"spark_{row.get('ClusterId_s', 'unknown')}",
                        job_id=row.get("SparkJobId_s"),
                        cluster_id=row.get("ClusterId_s"),
                    )
                )
            except Exception as e:
                logger.warning("executor_event_parse_error", error=str(e))

        return entries

    def _parse_synapse_failures(self, raw: Any) -> list[RawLogEntry]:
        """Parse Synapse pipeline failure entries."""
        entries = []
        rows = self._normalize_rows(raw)

        for row in rows:
            try:
                entries.append(
                    RawLogEntry(
                        timestamp=self._parse_timestamp(row.get("TimeGenerated")),
                        source=AlertSource.SYNAPSE,
                        level="ERROR",
                        message=row.get("ErrorMessage_s", "Pipeline failed"),
                        metadata={
                            "pipeline_name": row.get("PipelineName_s", ""),
                            "run_id": row.get("RunId_s", ""),
                            "activity": row.get("ActivityName_s", ""),
                            "activity_type": row.get("ActivityType_s", ""),
                            "duration": row.get("Duration_s", ""),
                        },
                        pipeline_name=row.get("PipelineName_s", "unknown_synapse_pipeline"),
                        job_id=row.get("RunId_s"),
                    )
                )
            except Exception as e:
                logger.warning("synapse_failure_parse_error", error=str(e))

        return entries

    def _parse_synapse_activities(self, raw: Any) -> list[RawLogEntry]:
        """Parse Synapse activity-level errors."""
        entries = []
        rows = self._normalize_rows(raw)

        for row in rows:
            try:
                entries.append(
                    RawLogEntry(
                        timestamp=self._parse_timestamp(row.get("TimeGenerated")),
                        source=AlertSource.SYNAPSE,
                        level="ERROR",
                        message=(
                            f"Activity '{row.get('ActivityName_s', '?')}' failed: "
                            f"{row.get('ErrorMessage_s', row.get('ErrorCode_s', 'Unknown'))}"
                        ),
                        metadata={
                            "pipeline_name": row.get("PipelineName_s", ""),
                            "activity_name": row.get("ActivityName_s", ""),
                            "activity_type": row.get("ActivityType_s", ""),
                            "error_code": row.get("ErrorCode_s", ""),
                            "duration": row.get("Duration_s", ""),
                        },
                        pipeline_name=row.get("PipelineName_s", "unknown"),
                    )
                )
            except Exception as e:
                logger.warning("synapse_activity_parse_error", error=str(e))

        return entries

    def _parse_generic_logs(self, raw: Any, category: str) -> list[RawLogEntry]:
        """Parse generic log entries with a category tag."""
        entries = []
        rows = self._normalize_rows(raw)

        for row in rows:
            try:
                message = row.get("Message_s") or row.get("ErrorMessage_s") or "Unknown error"
                entries.append(
                    RawLogEntry(
                        timestamp=self._parse_timestamp(row.get("TimeGenerated")),
                        source=AlertSource.LOG_ANALYTICS,
                        level="ERROR",
                        message=str(message),
                        metadata={"category": category, "type": row.get("Type", "")},
                        pipeline_name=row.get("PipelineName_s", f"unknown_{category}"),
                    )
                )
            except Exception as e:
                logger.warning("generic_log_parse_error", error=str(e))

        return entries

    @staticmethod
    def _normalize_rows(raw: Any) -> list[dict[str, Any]]:
        """Normalize raw response to list of dicts."""
        if isinstance(raw, list):
            return [r for r in raw if isinstance(r, dict)]
        if isinstance(raw, dict):
            tables = raw.get("tables", [])
            if tables:
                columns = [c.get("name", f"col_{i}") for i, c in enumerate(tables[0].get("columns", []))]
                return [dict(zip(columns, row)) for row in tables[0].get("rows", [])]
            return [raw]
        if isinstance(raw, str):
            return []
        return []

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        """Parse timestamp from various formats."""
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
        return datetime.utcnow()
