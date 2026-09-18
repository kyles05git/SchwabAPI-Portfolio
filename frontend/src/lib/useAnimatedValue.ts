import { useEffect, useRef, useState } from "react";

/**
 * Hook that animates a numeric value from old to new over a duration.
 * Returns the current interpolated value and a "flash" direction for color pulse.
 */
export function useAnimatedValue(
  target: number,
  duration: number = 600
): { value: number; flash: "up" | "down" | null } {
  const [display, setDisplay] = useState(target);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);
  const prevTarget = useRef(target);
  const animFrame = useRef<number | null>(null);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const from = prevTarget.current;
    const to = target;
    prevTarget.current = target;

    if (from === to) return;

    // Trigger flash
    setFlash(to > from ? "up" : "down");
    if (flashTimer.current) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => setFlash(null), 800);

    const startTime = performance.now();

    const animate = (now: number) => {
      const elapsed = now - startTime;
      const progress = Math.min(elapsed / duration, 1);
      // Ease-out cubic
      const eased = 1 - Math.pow(1 - progress, 3);
      const current = from + (to - from) * eased;
      setDisplay(current);

      if (progress < 1) {
        animFrame.current = requestAnimationFrame(animate);
      }
    };

    animFrame.current = requestAnimationFrame(animate);

    return () => {
      if (animFrame.current) cancelAnimationFrame(animFrame.current);
    };
  }, [target, duration]);

  return { value: display, flash };
}