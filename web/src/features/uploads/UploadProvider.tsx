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
  errorMessage,
  getJob,
  getUploadSession,
  jobEventsUrl,
  uploadSegment,
  WORKSPACE_ID,
} from "../../api/client";
import type { ParseJobView, UploadSessionView } from "../../api/contracts";
import { useWorkspace } from "../../app/workspace";
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
  retry: (taskId: string) => void;
  cancel: (taskId: string) => void;
  dismissFinished: () => void;
}

const UploadContext = createContext<UploadContextValue | null>(null);

export function UploadProvider({ children }: PropsWithChildren) {
  const { selectedSeries, refreshSeries } = useWorkspace();
  const [tasks, setTasks] = useState<UploadTask[]>([]);
  const [dialogOpen, setDialogOpen] = useState(false);
  const controllers = useRef(new Map<string, AbortController>());
  const running = useRef(new Set<string>());

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
        error: job.status === "FAILED" ? job.lastError || "解析任务失败" : undefined,
      }));
    },
    [updateTask],
  );

  const watchJob = useCallback(
    (taskId: string, initialJob: ParseJobView | { jobId: string }) => {
      const source = new EventSource(jobEventsUrl(initialJob.jobId));
      let recovering = false;

      source.addEventListener("job", (event) => {
        const job = JSON.parse((event as MessageEvent).data) as ParseJobView;
        acceptJob(taskId, job);
        if (job.status === "SUCCEEDED" || job.status === "FAILED") {
          source.close();
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
            source.close();
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

  const runRealTask = useCallback(
    async (task: UploadTask, signal: AbortSignal) => {
      if (!task.file) throw new Error("恢复上传需要重新选择原视频文件");
      const mediaType = mediaTypeForFile(task.file);
      if (!mediaType) throw new Error("只支持 MP4、MOV 和 AVI 视频");
      updateTask(task.taskId, (current) => ({ ...current, status: "creating", error: undefined }));

      let session: UploadSessionView;
      if (task.session) {
        session = await getUploadSession(task.session.uploadSessionId);
      } else {
        session = await createUploadSession(task.seriesId, task.idempotencyKey, {
          workspaceId: WORKSPACE_ID,
          episodeOrder: task.episodeOrder,
          uploadSequence: task.episodeOrder,
          originalFilename: task.file.name,
          mediaType,
          totalSizeBytes: task.file.size,
        });
      }

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
        const pendingBytes = [...inFlight.values()].reduce((sum, bytes) => sum + bytes, 0);
        updateTask(task.taskId, (current) => ({
          ...current,
          confirmedBytes: confirmed,
          inFlightBytes: pendingBytes,
        }));
      };

      const worker = async () => {
        while (cursor < missing.length) {
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
      const verified = await getUploadSession(session.uploadSessionId);
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
      updateTask(task.taskId, (current) => ({
        ...current,
        session: completed.uploadSession,
        status: "processing",
      }));
      watchJob(task.taskId, { jobId: completed.jobId });
    },
    [updateTask, watchJob],
  );

  const runTask = useCallback(
    async (task: UploadTask) => {
      if (running.current.has(task.taskId)) return;
      running.current.add(task.taskId);
      const controller = new AbortController();
      controllers.current.set(task.taskId, controller);
      try {
        await runRealTask(task, controller.signal);
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          updateTask(task.taskId, (current) => ({
            ...current,
            status: "failed",
            inFlightBytes: 0,
            error: errorMessage(error),
          }));
        }
      } finally {
        running.current.delete(task.taskId);
        controllers.current.delete(task.taskId);
      }
    },
    [runRealTask, updateTask],
  );

  useEffect(() => {
    const active = tasks.filter(
      (task) => ["creating", "uploading", "completing"].includes(task.status),
    ).length;
    const available = Math.max(0, MAX_PARALLEL_FILES - active);
    tasks
      .filter((task) => task.status === "queued")
      .slice(0, available)
      .forEach((task) => void runTask(task));
  }, [runTask, tasks]);

  useEffect(
    () => () => {
      controllers.current.forEach((controller) => controller.abort());
    },
    [],
  );

  const enqueue = useCallback(
    (inputs: UploadInput[]) => {
      if (!selectedSeries || inputs.length === 0) return;
      const additions = inputs.map<UploadTask>(({ file, episodeOrder }) => ({
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
      }));
      setTasks((current) => [...current, ...additions]);
    },
    [selectedSeries],
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

  const cancel = useCallback(
    (taskId: string) => {
      updateTask(taskId, (task) => ({ ...task, status: "cancelled", inFlightBytes: 0 }));
      controllers.current.get(taskId)?.abort();
    },
    [updateTask],
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
      openDialog: () => setDialogOpen(true),
      closeDialog: () => setDialogOpen(false),
      enqueue,
      pause,
      resume,
      retry: resume,
      cancel,
      dismissFinished: () =>
        setTasks((current) =>
          current.filter((task) => !["completed", "cancelled"].includes(task.status)),
        ),
    }),
    [cancel, dialogOpen, enqueue, overallProgress, pause, resume, tasks, visibleTasks],
  );

  return <UploadContext.Provider value={value}>{children}</UploadContext.Provider>;
}

export function useUploads(): UploadContextValue {
  const value = useContext(UploadContext);
  if (!value) throw new Error("useUploads must be used inside UploadProvider");
  return value;
}
