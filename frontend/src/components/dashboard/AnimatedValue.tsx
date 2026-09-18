import { useAnimatedValue } from "@/lib/useAnimatedValue";

interface AnimatedValueProps {
  value: number;
  formatter: (v: number) => string;
  className?: string;
}

/**
 * Displays a number that animates smoothly when it changes,
 * with a brief color flash (green for increase, red for decrease).
 */
export function AnimatedValue({ value, formatter, className = "" }: AnimatedValueProps) {
  const { value: display, flash } = useAnimatedValue(value);

  const flashClass =
    flash === "up"
      ? "animate-flash-up"
      : flash === "down"
      ? "animate-flash-down"
      : "";

  return (
    <span className={`tnum transition-colors duration-300 ${flashClass} ${className}`}>
      {formatter(display)}
    </span>
  );
}