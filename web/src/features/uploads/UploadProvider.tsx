import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  completeUpload,
  createUploadSession,
  deleteUploadSession,
  errorMessage,
  getJob,
  getUploadSession,
  getVideoParseTask,
  jobEventsUrl,
  uploadSegment,
  videoParseTaskEventsUrl,
  TENANT_ID,
} from "../../api/client";
import type {
  ParseJobView,
  UploadCompleteView,
  UploadSessionView,
  VideoParseTaskView,
} from "../../api/contracts";
import { useWorkspace } from "../../app/workspace";
import { useAuth } from "../auth/AuthProvider";
import type { UploadInput, UploadTask } from "./upload-types";
import { mediaTypeForFile, taskProgress } from "./upload-types";

const MAX_PARALLEL_FILES = 3;
const SEGMENT_WORKERS = 3;

interface UploadContextValue {
  tasks: UploadTask[];
  visibleTasks: UploadTask[];
  activeCount: number;
  overallProgress: number;
  dialogOpen: boolean;
  openDialog: () => void;
  closeDialog: () => void;
  enqueue: (files: UploadInput[]) => void;
  pause: (taskId: string) => void;
  resume: (taskId: string) => void;
  attachFile: (taskId: string, file: File) => void;
  retry: (taskId: string) => void;
  cancel: (taskId: string) => Promise<void>;
  forgetDeletedSession: (uploadSessionId: string) => void;
  syncRequeuedParse: (task: ParseJobView | VideoParseTaskView) => void;
  dismissFinished: () => void;
}

const UploadContext = createContext<UploadContextValue | null>(null);

export function UploadProvider({ children }: PropsWithChildren) {
  const { selectedSeries, refreshSeries } = useWorkspace();
  const { canUpload } = useAuth();
  const [tasks, setTasks] = useState<UploadTask[]>([]);
  const [dialogOpen, setDialogOpen] = useState(false);
  const controllers = useRef(new Map<string, AbortController>());
  const running = useRef(new Map<string, Promise<void>>());
  const sessions = useRef(new Map<string, UploadSessionView>());
  const jobSources = useRef(new Map<string, EventSource>());

  const updateTask = useCallback(
    (taskId: string, updater: (task: UploadTask) => UploadTask) => {
      setTasks((current) =>
        current.map((task) => (task.taskId === taskId ? updater(task) : task)),
      );
    },
    [],
  );

  const acceptJob = useCallback(
    (taskId: string, job: ParseJobView) => {
      updateTask(taskId, (task) => ({
        ...task,
        job,
        status:
          job.status === "SUCCEEDED"
            ? "completed"
            : job.status === "FAILED"
              ? "failed"
              : "processing",
        error: job.status === "FAILED" ? job.lastError?.message || "解析任务失败" : undefined,
      }));
    },
    [updateTask],
  );

  const acceptVideoParseTask = useCallback(
    (taskId: string, videoParseTask: VideoParseTaskView) => {
      const failed = videoParseTask.status === "PARSING_FAILED"
        || videoParseTask.status === "INDEXING_FAILED";
      updateTask(taskId, (task) => ({
        ...task,
        videoParseTask,
        status: videoParseTask.status === "COMPLETE"
          ? "completed"
          : failed
            ? "failed"
            : "processing",
        error: failed
          ? videoParseTask.lastError?.message || "解析任务失败"
          : undefined,
      }));
    },
    [updateTask],
  );

  const watchJob = useCallback(
    (taskId: string, initialJob: ParseJobView | { jobId: string }) => {
      jobSources.current.get(taskId)?.close();
      const source = new EventSource(jobEventsUrl(initialJob.jobId), { withCredentials: true });
      jobSources.current.set(taskId, source);
      let recovering = false;

      const close = () => {
        source.close();
        if (jobSources.current.get(taskId) === source) {
          jobSources.current.delete(taskId);
        }
      };

      source.addEventListener("job", (event) => {
        const job = JSON.parse((event as MessageEvent).data) as ParseJobView;
        acceptJob(taskId, job);
        if (job.status === "SUCCEEDED" || job.status === "FAILED") {
          close();
          void refreshSeries();
        }
      });

      source.onerror = async () => {
        if (recovering) return;
        recovering = true;
        try {
          const job = await getJob(initialJob.jobId);
          acceptJob(taskId, job);
          if (job.status === "SUCCEEDED" || job.status === "FAILED") {
            close();
            void refreshSeries();
          }
        } catch {
          // EventSource reconnects automatically. The next successful snapshot is authoritative.
        } finally {
          recovering = false;
        }
      };
    },
    [acceptJob, refreshSeries],
  );

  const watchVideoParseTask = useCallback(
    (taskId: string, initialTask: VideoParseTaskView | { videoParseTaskId: string }) => {
      jobSources.current.get(taskId)?.close();
      const source = new EventSource(
        videoParseTaskEventsUrl(initialTask.videoParseTaskId),
        { withCredentials: true },
      );
      jobSources.current.set(taskId, source);
      let recovering = false;

      const close = () => {
        source.close();
        if (jobSources.current.get(taskId) === source) {
          jobSources.current.delete(taskId);
        }
      };
      const accept = (videoParseTask: VideoParseTaskView) => {
        acceptVideoParseTask(taskId, videoParseTask);
        if (
          videoParseTask.status === "COMPLETE"
          || videoParseTask.status === "PARSING_FAILED"
          || videoParseTask.status === "INDEXING_FAILED"
        ) {
          close();
          void refreshSeries();
        }
      };

      source.addEventListener("video-parse-task", (event) => {
        accept(JSON.parse((event as MessageEvent).data) as VideoParseTaskView);
      });
      source.onerror = async () => {
        if (recovering) return;
        recovering = true;
        try {
          accept(await getVideoParseTask(initialTask.videoParseTaskId));
        } catch {
          // EventSource reconnects automatically. The next successful snapshot is authoritative.
        } finally {
          recovering = false;
        }
      };
    },
    [acceptVideoParseTask, refreshSeries],
  );

  const watchCompletedParse = useCallback(
    (taskId: string, completed: UploadCompleteView) => {
      if (completed.jobId) {
        watchJob(taskId, { jobId: completed.jobId });
        return;
      }
      watchVideoParseTask(taskId, {
        videoParseTaskId: completed.videoParseTaskId,
      });
    },
    [watchJob, watchVideoParseTask],
  );

  const runRealTask = useCallback(
    async (task: UploadTask, signal: AbortSignal) => {
      updateTask(task.taskId, (current) => ({ ...current, status: "creating", error: undefined }));

      let session: UploadSessionView;
      const existingSession = sessions.current.get(task.taskId) ?? task.session;
      if (existingSession) {
        session = await getUploadSession(existingSession.uploadSessionId, signal);
      } else {
        if (!task.file) throw new Error("恢复上传需要重新选择原视频文件");
        const mediaType = mediaTypeForFile(task.file);
        if (!mediaType) throw new Error("只支持 MP4、MOV 和 AVI 视频");
        session = await createUploadSession(task.seriesId, task.idempotencyKey, {
          tenantId: TENANT_ID,
          episodeOrder: task.episodeOrder,
          originalFilename: task.file.name,
          mediaType,
          totalSizeBytes: task.file.size,
          parseConfig: task.parseConfig,
        });
      }

      sessions.current.set(task.taskId, session);
      updateTask(task.taskId, (current) => ({
        ...current, session, episodeOrder: session.episodeOrder,
      }));
      signal.throwIfAborted();
      if (session.seriesId !== task.seriesId) {
        throw new Error("上传会话不属于当前视频系列");
      }
      if (task.file && (
        session.originalFilename !== task.file.name ||
        session.totalSizeBytes !== task.file.size
      )) {
        throw new Error("上传会话与当前文件不一致，请重新选择视频");
      }
      if (session.status === "EXPIRED") {
        throw new Error("上传会话已过期，请重新加入这个视频");
      }
      if (session.status === "COMPLETING") {
        updateTask(task.taskId, (current) => ({
          ...current,
          session,
          status: "completing",
          confirmedBytes: task.totalBytes,
          inFlightBytes: 0,
        }));
        const completed = await completeUpload(session.uploadSessionId);
        signal.throwIfAborted();
        updateTask(task.taskId, (current) => ({
          ...current,
          session: completed.uploadSession,
          status: "processing",
        }));
        watchCompletedParse(task.taskId, completed);
        return;
      }
      if (session.status === "COMPLETED") {
        const completed = await completeUpload(session.uploadSessionId);
        signal.throwIfAborted();
        updateTask(task.taskId, (current) => ({
          ...current,
          session: completed.uploadSession,
          status: "processing",
          confirmedBytes: task.totalBytes,
          inFlightBytes: 0,
        }));
        watchCompletedParse(task.taskId, completed);
        return;
      }
      if (!task.file) throw new Error("恢复上传需要重新选择原视频文件");
      if (!mediaTypeForFile(task.file)) throw new Error("只支持 MP4、MOV 和 AVI 视频");

      updateTask(task.taskId, (current) => ({
        ...current,
        session,
        status: "uploading",
        confirmedBytes: session.uploadedSizeBytes,
        inFlightBytes: 0,
      }));

      const missing = [...session.missingSegmentNumbers];
      let cursor = 0;
      let confirmed = session.uploadedSizeBytes;
      const inFlight = new Map<number, number>();

      const syncProgress = () => {
        if (signal.aborted) return;
        const pendingBytes = [...inFlight.values()].reduce((sum, bytes) => sum + bytes, 0);
        updateTask(task.taskId, (current) => ({
          ...current,
          confirmedBytes: confirmed,
          inFlightBytes: pendingBytes,
        }));
      };

      const worker = async () => {
        while (cursor < missing.length) {
          signal.throwIfAborted();
          const segmentNumber = missing[cursor];
          cursor += 1;
          if (segmentNumber === undefined) return;
          inFlight.set(segmentNumber, 0);
          const size = await uploadSegment({
            file: task.file!,
            session,
            segmentNumber,
            signal,
            onProgress: (loadedBytes) => {
              inFlight.set(segmentNumber, loadedBytes);
              syncProgress();
            },
          });
          inFlight.delete(segmentNumber);
          confirmed += size;
          syncProgress();
        }
      };

      await Promise.all(
        Array.from({ length: Math.min(SEGMENT_WORKERS, missing.length) }, worker),
      );
      signal.throwIfAborted();
      const verified = await getUploadSession(session.uploadSessionId, signal);
      signal.throwIfAborted();
      if (verified.missingSegmentNumbers.length > 0) {
        throw new Error("仍有分片未被服务端确认，请重试上传");
      }
      updateTask(task.taskId, (current) => ({
        ...current,
        session: verified,
        status: "completing",
        confirmedBytes: task.totalBytes,
        inFlightBytes: 0,
      }));
      const completed = await completeUpload(session.uploadSessionId);
      signal.throwIfAborted();
      updateTask(task.taskId, (current) => ({
        ...current,
        session: completed.uploadSession,
        status: "processing",
      }));
      watchCompletedParse(task.taskId, completed);
    },
    [updateTask, watchCompletedParse],
  );

  const runTask = useCallback(
    (task: UploadTask) => {
      if (running.current.has(task.taskId)) return;
      const controller = new AbortController();
      controllers.current.set(task.taskId, controller);
      const execution = runRealTask(task, controller.signal).catch((error: unknown) => {
        if (!controller.signal.aborted) {
          controller.abort();
          updateTask(task.taskId, (current) => ({
            ...current,
            status: "failed",
            inFlightBytes: 0,
            error: errorMessage(error),
          }));
        }
      }).finally(() => {
        running.current.delete(task.taskId);
        controllers.current.delete(task.taskId);
        setTasks((current) => [...current]);
      });
      running.current.set(task.taskId, execution);
      return execution;
    },
    [runRealTask, updateTask],
  );

  useEffect(() => {
    if (!canUpload || !selectedSeries) return;
    setTasks((current) => {
      const known = new Set(current.map((task) =>
        (sessions.current.get(task.taskId) ?? task.session)?.uploadSessionId,
      ));
      const recovered = selectedSeries.pendingUploads.filter((session) => !known.has(session.uploadSessionId));
      const pendingIds = new Set(selectedSeries.pendingUploads.map((session) => session.uploadSessionId));
      const retained = current.filter((task) => task.file || task.status !== "completing" || pendingIds.has(task.session!.uploadSessionId));
      if (recovered.length === 0 && retained.length === current.length) return current;
      return [...retained, ...recovered.map<UploadTask>((session) => ({
        taskId: session.uploadSessionId,
        seriesId: session.seriesId,
        fileName: session.originalFilename,
        episodeOrder: session.episodeOrder,
        totalBytes: session.totalSizeBytes,
        confirmedBytes: session.uploadedSizeBytes,
        inFlightBytes: 0,
        status: session.status === "COMPLETING" ? "completing" : "paused",
        idempotencyKey: "",
        session,
        error: session.status === "COMPLETING" ? undefined : session.lastError?.message ?? "请重新选择同一个原视频，系统会从服务端已确认的分片继续上传",
      }))];
    });
  }, [canUpload, selectedSeries]);

  useEffect(() => {
    if (!canUpload) return;
    const active = tasks.filter(
      (task) => ["creating", "uploading", "completing"].includes(task.status),
    ).length;
    const available = Math.max(0, MAX_PARALLEL_FILES - active);
    tasks
      .filter((task) => task.status === "queued")
      .slice(0, available)
      .forEach((task) => void runTask(task));
  }, [canUpload, runTask, tasks]);

  useEffect(
    () => () => {
      controllers.current.forEach((controller) => controller.abort());
      jobSources.current.forEach((source) => source.close());
    },
    [],
  );

  const enqueue = useCallback(
    (inputs: UploadInput[]) => {
      if (!canUpload || !selectedSeries || inputs.length === 0) return;
      const additions = inputs.map<UploadTask>(({ file, episodeOrder, parseConfig }) => ({
        taskId: crypto.randomUUID(),
        seriesId: selectedSeries.seriesId,
        file,
        fileName: file.name,
        episodeOrder,
        totalBytes: file.size,
        confirmedBytes: 0,
        inFlightBytes: 0,
        status: "queued",
        idempotencyKey: crypto.randomUUID(),
        parseConfig,
      }));
      setTasks((current) => [...current, ...additions]);
    },
    [canUpload, selectedSeries],
  );

  const pause = useCallback(
    (taskId: string) => {
      updateTask(taskId, (task) => ({ ...task, status: "paused", inFlightBytes: 0 }));
      controllers.current.get(taskId)?.abort();
    },
    [updateTask],
  );

  const resume = useCallback(
    (taskId: string) => {
      updateTask(taskId, (task) => ({
        ...task,
        status: "queued",
        error: undefined,
      }));
    },
    [updateTask],
  );

  const attachFile = useCallback(
    (taskId: string, file: File) => {
      updateTask(taskId, (task) => {
        if (!mediaTypeForFile(file)) {
          return { ...task, error: "只支持 MP4、MOV 和 AVI 视频" };
        }
        if (file.name !== task.fileName || file.size !== task.totalBytes) {
          return { ...task, error: "所选文件与原上传任务的文件名或大小不一致" };
        }
        return { ...task, file, status: "queued", error: undefined };
      });
    },
    [updateTask],
  );

  const cancel = useCallback(
    async (taskId: string) => {
      const task = tasks.find((item) => item.taskId === taskId);
      if (!task || task.session?.status === "COMPLETED" || ["deleting", "cancelled", "completing", "processing", "completed"].includes(task.status)) return;
      updateTask(taskId, (current) => ({ ...current, status: "deleting", inFlightBytes: 0, error: undefined }));
      controllers.current.get(taskId)?.abort();
      await running.current.get(taskId);
      try {
        let session = sessions.current.get(taskId) ?? task.session;
        const mediaType = task.file ? mediaTypeForFile(task.file) : null;
        if (!session && task.file && mediaType && task.status !== "queued") {
          // Replay an uncertain creation response before removing its server session.
          session = await createUploadSession(task.seriesId, task.idempotencyKey, {
            tenantId: TENANT_ID, episodeOrder: task.episodeOrder,
            originalFilename: task.file.name, mediaType, totalSizeBytes: task.file.size,
            parseConfig: task.parseConfig,
          });
          sessions.current.set(taskId, session);
        }
        if (session) await deleteUploadSession(session.uploadSessionId);
        sessions.current.delete(taskId);
        jobSources.current.get(taskId)?.close();
        jobSources.current.delete(taskId);
        setTasks((current) => current.filter((item) => item.taskId !== taskId));
        void refreshSeries();
      } catch (error) {
        updateTask(taskId, (current) => ({ ...current, status: "failed", error: `删除失败：${errorMessage(error)}` }));
      }
    },
    [refreshSeries, tasks, updateTask],
  );

  const forgetDeletedSession = useCallback(
    (uploadSessionId: string) => {
      const taskIds = new Set(
        tasks
          .filter((task) =>
            (sessions.current.get(task.taskId) ?? task.session)?.uploadSessionId
              === uploadSessionId,
          )
          .map((task) => task.taskId),
      );
      for (const taskId of taskIds) {
        controllers.current.get(taskId)?.abort();
        sessions.current.delete(taskId);
        jobSources.current.get(taskId)?.close();
        jobSources.current.delete(taskId);
      }
      setTasks((current) => current.filter((task) => !taskIds.has(task.taskId)));
    },
    [tasks],
  );

  const syncRequeuedParse = useCallback(
    (parseTask: ParseJobView | VideoParseTaskView) => {
      const uploadSessionId = parseTask.uploadSessionId;
      if (!uploadSessionId) return;
      const task = tasks.find((item) =>
        (sessions.current.get(item.taskId) ?? item.session)?.uploadSessionId
          === uploadSessionId,
      );
      if (!task) return;
      updateTask(task.taskId, (current) => ({
        ...current,
        ...("jobId" in parseTask
          ? { job: parseTask }
          : { videoParseTask: parseTask }),
        status: "processing",
        error: undefined,
      }));
      if ("jobId" in parseTask) {
        watchJob(task.taskId, parseTask);
      } else {
        watchVideoParseTask(task.taskId, parseTask);
      }
    },
    [tasks, updateTask, watchJob, watchVideoParseTask],
  );

  const visibleTasks = tasks.filter((task) => task.status !== "cancelled");
  const overallProgress =
    visibleTasks.length === 0
      ? 0
      : Math.round(
          visibleTasks.reduce((sum, task) => sum + taskProgress(task), 0) /
            visibleTasks.length,
        );

  const value = useMemo<UploadContextValue>(
    () => ({
      tasks,
      visibleTasks,
      activeCount: visibleTasks.filter((task) =>
        ["queued", "creating", "uploading", "completing", "processing"].includes(
          task.status,
        ),
      ).length,
      overallProgress,
      dialogOpen,
      openDialog: () => { if (canUpload) setDialogOpen(true); },
      closeDialog: () => setDialogOpen(false),
      enqueue,
      pause,
      resume,
      attachFile,
      retry: resume,
      cancel,
      forgetDeletedSession,
      syncRequeuedParse,
      dismissFinished: () =>
        setTasks((current) =>
          current.filter((task) => !["completed", "cancelled"].includes(task.status)),
        ),
    }),
    [attachFile, cancel, canUpload, dialogOpen, enqueue, forgetDeletedSession, overallProgress, pause, resume, syncRequeuedParse, tasks, visibleTasks],
  );

  return <UploadContext.Provider value={value}>{children}</UploadContext.Provider>;
}

export function useUploads(): UploadContextValue {
  const value = useContext(UploadContext);
  if (!value) throw new Error("useUploads must be used inside UploadProvider");
  return value;
}
