import React, { useEffect, useRef, useState } from "react";
import { MaterialDTO, RationalTime, PROXY_480P } from "@aether/contracts";

interface CanvasPreviewProps {
  currentTime: RationalTime;
  onTimeChange: (time: RationalTime) => void;
  timelineDuration: RationalTime;
  previewMaterial: MaterialDTO | null;
}

export const CanvasPreview: React.FC<CanvasPreviewProps> = ({
  currentTime,
  onTimeChange,
  timelineDuration,
  previewMaterial,
}) => {
  const [isPlaying, setIsPlaying] = useState(false);
  const mediaRef = useRef<HTMLVideoElement | HTMLAudioElement | null>(null);

  useEffect(() => {
    let interval: ReturnType<typeof setInterval> | undefined;
    if (isPlaying && !previewMaterial) {
      interval = setInterval(() => {
        const frameTime = new RationalTime(1000, 24000);
        const nextTime = currentTime.add(frameTime);
        const hasDuration = timelineDuration.greaterThan(new RationalTime(0, 1));
        if (!nextTime.lessThan(timelineDuration) && hasDuration) {
          setIsPlaying(false);
          onTimeChange(timelineDuration);
        } else {
          onTimeChange(nextTime);
        }
      }, 41.67);
    }
    return () => {
      if (interval !== undefined) clearInterval(interval);
    };
  }, [isPlaying, currentTime, timelineDuration, onTimeChange, previewMaterial]);

  const handlePlayPause = async () => {
    const media = mediaRef.current;
    if (media) {
      if (media.paused) {
        try {
          await media.play();
        } catch {
          setIsPlaying(false);
        }
      } else {
        media.pause();
      }
      return;
    }
    setIsPlaying((playing) => !playing);
  };

  const handleRewind = () => {
    setIsPlaying(false);
    if (mediaRef.current) mediaRef.current.currentTime = 0;
    onTimeChange(new RationalTime(0, 24000));
  };

  const currentSeconds = currentTime.toSeconds();
  const totalSeconds = timelineDuration.toSeconds();

  return (
    <div className="panel" style={{ flex: 1, height: "100%", background: "#0c0c0e", borderRight: "none" }}>
      <div className="panel-header">画面监看 · 480p 代理目标</div>
      <div className="canvas-panel" style={{ height: "100%", justifyContent: "space-between" }}>
        <div className="canvas-viewport" style={{ flex: 1, width: "100%" }}>
          <div className="proxy-badge">
            {PROXY_480P.width}×{PROXY_480P.height} · {PROXY_480P.fps} 帧/秒 · {PROXY_480P.codec.toUpperCase()}
          </div>

          {previewMaterial?.type === "video" ? (
            <video
              key={previewMaterial.id}
              ref={mediaRef as React.RefObject<HTMLVideoElement>}
              className="media-preview"
              src={previewMaterial.url}
              controls
              preload="metadata"
              aria-label={`预览素材 ${previewMaterial.name}`}
              onPlay={() => setIsPlaying(true)}
              onPause={() => setIsPlaying(false)}
              onEnded={() => setIsPlaying(false)}
              onTimeUpdate={(event) => {
                if (totalSeconds > 0) {
                  onTimeChange(RationalTime.fromSeconds(Math.min(event.currentTarget.currentTime, totalSeconds), 24000));
                }
              }}
            />
          ) : previewMaterial?.type === "audio" ? (
            <div className="audio-preview">
              <div>当前音频：{previewMaterial.name}</div>
              <audio
                key={previewMaterial.id}
                ref={mediaRef as React.RefObject<HTMLAudioElement>}
                src={previewMaterial.url}
                controls
                preload="metadata"
                aria-label={`预览音频 ${previewMaterial.name}`}
                onPlay={() => setIsPlaying(true)}
                onPause={() => setIsPlaying(false)}
                onEnded={() => setIsPlaying(false)}
                onTimeUpdate={(event) => {
                  if (totalSeconds > 0) {
                    onTimeChange(RationalTime.fromSeconds(Math.min(event.currentTarget.currentTime, totalSeconds), 24000));
                  }
                }}
              />
            </div>
          ) : (
            <div className="preview-empty-state">
              <strong>暂无可预览素材</strong>
              <span>上传视频或音频并添加到时间线后，可在这里播放原始素材。</span>
            </div>
          )}

          {previewMaterial && (
            <div className="preview-caption">
              原始素材预览：{previewMaterial.name} · 最终合成效果以渲染成片为准
            </div>
          )}
        </div>

        <div style={{ width: "100%", marginTop: "16px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "10px", width: "100%" }}>
            <input
              aria-label="时间线位置"
              type="range"
              min={0}
              max={Math.max(totalSeconds, 1)}
              step={0.0416}
              value={Math.min(currentSeconds, Math.max(totalSeconds, 1))}
              disabled={totalSeconds <= 0}
              onChange={(event) => {
                const seconds = Number.parseFloat(event.target.value);
                onTimeChange(RationalTime.fromSeconds(seconds, 24000));
                const media = mediaRef.current;
                if (media && Number.isFinite(media.duration)) {
                  media.currentTime = Math.min(seconds, media.duration);
                }
              }}
            />
          </div>

          <div className="canvas-controls" style={{ margin: "12px 0 0 0" }}>
            <button className="secondary" onClick={handleRewind}>回到开头</button>
            <button onClick={handlePlayPause}>{isPlaying ? "暂停" : "播放"}</button>
            <div className="time-readout">
              <div>{currentSeconds.toFixed(3)} 秒 / {totalSeconds.toFixed(3)} 秒</div>
              <div className="rational-readout">
                精确时间：{currentTime.value} / {currentTime.timescale}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
