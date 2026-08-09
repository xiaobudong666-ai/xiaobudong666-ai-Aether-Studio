import React from "react";
import { TimelineDTO, ClipDTO, RationalTime } from "@aether/contracts";
import { localizeTrackName, trackTypeLabel } from "../i18n";

interface TimelineProps {
  timeline: TimelineDTO;
  selectedClipId: string | null;
  onSelectClip: (clip: ClipDTO) => void;
  currentTime: RationalTime;
}

export const Timeline: React.FC<TimelineProps> = ({
  timeline,
  selectedClipId,
  onSelectClip,
  currentTime,
}) => {
  // Approximate conversion factor: 1 second = 30px width
  const PIXELS_PER_SECOND = 40;

  const currentSeconds = currentTime.toSeconds();

  const handleClipClick = (e: React.MouseEvent, clip: ClipDTO) => {
    e.stopPropagation();
    onSelectClip(clip);
  };

  return (
    <div className="timeline-panel">
      {/* Timeline Controls / Ruler Header */}
      <div style={{ height: "32px", background: "#1a1a1e", borderBottom: "1px solid #2e2e33", display: "flex", alignItems: "center", padding: "0 12px", justifyContent: "space-between" }}>
        <div style={{ display: "flex", gap: "12px", alignItems: "center", fontSize: "12px", fontWeight: "bold", color: "#d4d4d8" }}>
          <span>时间线轨道（标准格式 v1.1）</span>
        </div>
        <div style={{ fontSize: "12px", color: "#a1a1aa" }}>
          比例：{PIXELS_PER_SECOND} 像素/秒 · 时间：{currentSeconds.toFixed(3)} 秒
        </div>
      </div>

      {/* Tracks List */}
      <div style={{ flex: 1, overflowY: "auto", overflowX: "auto", position: "relative" }}>

        {/* Playhead Overlay */}
        <div
          style={{
            position: "absolute",
            top: 0,
            bottom: 0,
            left: `${currentSeconds * PIXELS_PER_SECOND + 120}px`, // Offset by track title width (120px)
            width: "2px",
            backgroundColor: "#ef4444",
            zIndex: 10,
            pointerEvents: "none",
          }}
        >
          <div style={{ width: "8px", height: "8px", borderRadius: "50%", backgroundColor: "#ef4444", position: "absolute", top: 0, left: "-3px" }} />
        </div>

        {timeline.tracks.length === 0 ? (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "#a1a1aa", fontStyle: "italic", fontSize: "13px" }}>
            还没有轨道。把素材添加到时间线后会自动创建对应轨道。
          </div>
        ) : (
          timeline.tracks.map((track) => (
            <div
              key={track.id}
              style={{
                display: "flex",
                alignItems: "center",
                borderBottom: "1px solid #1e1e24",
                height: "50px",
                minWidth: "100%",
                boxSizing: "border-box",
              }}
            >
              {/* Track Title Panel */}
              <div
                style={{
                  width: "120px",
                  background: "#16161a",
                  borderRight: "1px solid #2e2e33",
                  height: "100%",
                  display: "flex",
                  flexDirection: "column",
                  justifyContent: "center",
                  padding: "0 8px",
                  boxSizing: "border-box",
                  fontSize: "12px",
                  fontWeight: 600,
                  color: "#d4d4d8",
                  zIndex: 2,
                }}
              >
                <div>{localizeTrackName(track.name, track.type)}</div>
                <div style={{ fontSize: "12px", color: "#a1a1aa" }}>{trackTypeLabel(track.type)}</div>
              </div>

              {/* Track Body / Clips viewport */}
              <div
                style={{
                  flex: 1,
                  height: "100%",
                  background: "#0c0c0e",
                  position: "relative",
                  display: "flex",
                  alignItems: "center",
                }}
              >
                {track.clips.map((clip) => {
                  const clipStartSec = clip.start.value / clip.start.timescale;
                  const clipDurSec = clip.duration.value / clip.duration.timescale;
                  const left = clipStartSec * PIXELS_PER_SECOND;
                  const width = clipDurSec * PIXELS_PER_SECOND;

                  const isSelected = selectedClipId === clip.id;

                  return (
                    <button
                      type="button"
                      key={clip.id}
                      onClick={(e) => handleClipClick(e, clip)}
                      aria-label={`选择片段，时长 ${clipDurSec.toFixed(2)} 秒`}
                      style={{
                        position: "absolute",
                        left: `${left}px`,
                        width: `${Math.max(width, 10)}px`,
                        height: "36px",
                        background: track.type === "video" ? "linear-gradient(135deg, #4f46e5, #4338ca)" : track.type === "audio" ? "linear-gradient(135deg, #059669, #047857)" : "linear-gradient(135deg, #b45309, #92400e)",
                        border: isSelected ? "2px solid #fff" : "1px solid rgba(255,255,255,0.15)",
                        borderRadius: "4px",
                        boxSizing: "border-box",
                        cursor: "pointer",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        padding: "0 6px",
                        overflow: "hidden",
                        zIndex: isSelected ? 5 : 3,
                        boxShadow: "0 2px 4px rgba(0,0,0,0.3)",
                      }}
                    >
                      <div style={{ fontSize: "12px", fontWeight: "bold", color: "#fff", whiteSpace: "nowrap", textOverflow: "ellipsis", overflow: "hidden" }}>
                        片段（{clipDurSec.toFixed(2)} 秒）
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
};
