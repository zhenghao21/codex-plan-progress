// C# 5 / .NET Framework WPF. The controller owns task state; this process only
// renders it and saves user placement. No native window resizes during motion.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;

namespace CodexPlanProgress
{
    public sealed class MotionState
    {
        public double Value, Velocity;
        public MotionState() { }
        public MotionState(double value, double velocity) { Value = value; Velocity = velocity; }
    }
    public static class Motion
    {
        public static MotionState Sample(double value, double velocity, double target, double dt)
        {
            dt = Math.Max(0, dt);
            const double omega = 24;
            double error = value - target, coefficient = velocity + omega * error;
            double decay = Math.Exp(-omega * dt);
            return new MotionState(target + (error + coefficient * dt) * decay,
                (velocity - omega * coefficient * dt) * decay);
        }
    }
    public sealed class TaskNode
    {
        public string Step, Status, Key;
        public List<TaskNode> Children = new List<TaskNode>();
    }
    public sealed class Counts { public int Completed, Total; }
    public static class PlanModel
    {
        public static void ValidateAndKey(IList<TaskNode> nodes)
        {
            int count = 0;
            Validate(nodes, "", 1, ref count);
        }
        private static void Validate(IList<TaskNode> nodes, string prefix, int depth, ref int count)
        {
            if (nodes == null || depth > 6 && nodes.Count != 0) throw new FormatException("Invalid task depth");
            for (int i = 0; i < nodes.Count; i++)
            {
                TaskNode node = nodes[i];
                if (++count > 100 || node == null || String.IsNullOrWhiteSpace(node.Step) || node.Step.Length > 2000 ||
                    !(node.Status == "pending" || node.Status == "in_progress" || node.Status == "completed" || node.Status == "blocked"))
                    throw new FormatException("Invalid task");
                node.Key = prefix + "/" + i.ToString(CultureInfo.InvariantCulture) + ":" + node.Step.Length + ":" + node.Step;
                Validate(node.Children, node.Key, depth + 1, ref count);
            }
        }
        public static Counts Count(IList<TaskNode> nodes)
        {
            Counts count = new Counts();
            foreach (TaskNode node in nodes)
            {
                Counts children = Count(node.Children);
                count.Total += 1 + children.Total;
                count.Completed += (node.Status == "completed" ? 1 : 0) + children.Completed;
            }
            return count;
        }
        public static IEnumerable<TaskNode> Walk(IList<TaskNode> nodes)
        {
            foreach (TaskNode node in nodes)
            {
                yield return node;
                foreach (TaskNode child in Walk(node.Children)) yield return child;
            }
        }
    }
    public sealed class RowLayout
    {
        public TaskNode Node;
        public string Key;
        public double Top, NaturalHeight, ClipHeight;
    }
    public sealed class LayoutResult
    {
        public List<RowLayout> Rows = new List<RowLayout>();
        public Dictionary<string, double> ChildExtents = new Dictionary<string, double>();
        public double Height;
    }
    public static class PlanLayout
    {
        public static LayoutResult Build(IList<TaskNode> nodes, IDictionary<string, double> naturalHeights,
            IDictionary<string, MotionState> expansions)
        {
            LayoutResult result = new LayoutResult();
            Dictionary<string, double> totals = new Dictionary<string, double>();
            Func<TaskNode, double> measure = null;
            measure = delegate(TaskNode node)
            {
                double children = 0;
                foreach (TaskNode child in node.Children) children += measure(child);
                result.ChildExtents[node.Key] = children;
                MotionState state;
                double expansion = expansions.TryGetValue(node.Key, out state) ? Math.Max(0, Math.Min(1, state.Value)) : 0;
                double natural;
                if (!naturalHeights.TryGetValue(node.Key, out natural) || natural <= 0) natural = 22;
                return totals[node.Key] = natural + 2 + children * expansion;
            };
            foreach (TaskNode node in nodes) result.Height += measure(node);
            Action<TaskNode, double, double> place = null;
            place = delegate(TaskNode node, double top, double clipEnd)
            {
                double natural;
                if (!naturalHeights.TryGetValue(node.Key, out natural) || natural <= 0) natural = 22;
                double rowTop = top + 1;
                result.Rows.Add(new RowLayout { Node = node, Key = node.Key, Top = rowTop,
                    NaturalHeight = natural, ClipHeight = Math.Max(0, Math.Min(natural, clipEnd - rowTop)) });
                MotionState motion;
                double expansion = expansions.TryGetValue(node.Key, out motion) ? Math.Max(0, Math.Min(1, motion.Value)) : 0;
                double childTop = top + natural + 2;
                double childEnd = Math.Min(clipEnd, childTop + result.ChildExtents[node.Key] * expansion);
                foreach (TaskNode child in node.Children) { place(child, childTop, childEnd); childTop += totals[child.Key]; }
            };
            double y = 0;
            foreach (TaskNode node in nodes) { place(node, y, Double.PositiveInfinity); y += totals[node.Key]; }
            return result;
        }
    }
    public sealed class ChatViewState
    {
        public bool Collapsed;
        public double Scroll;
        public HashSet<string> Expanded = new HashSet<string>();
    }
    public sealed class ViewMemory
    {
        private readonly Dictionary<string, ChatViewState> states = new Dictionary<string, ChatViewState>();
        public ChatViewState Get(string chat)
        {
            ChatViewState state;
            if (!states.TryGetValue(chat ?? "", out state)) states[chat ?? ""] = state = new ChatViewState();
            return state;
        }
    }
    public sealed class PointD
    {
        public double X, Y;
        public PointD() { }
        public PointD(double x, double y) { X = x; Y = y; }
    }
    public sealed class RectD
    {
        public double Left, Top, Right, Bottom;
        public RectD() { }
        public RectD(double left, double top, double right, double bottom) { Left = left; Top = top; Right = right; Bottom = bottom; }
    }
    public static class DragMath
    {
        public static PointD Clamp(PointD position, double width, double height, RectD bounds)
        {
            return new PointD(Math.Max(bounds.Left + 8, Math.Min(bounds.Right - width - 8, position.X)),
                Math.Max(bounds.Top + 8, Math.Min(bounds.Bottom - height - 8, position.Y)));
        }
        public static PointD Move(PointD current, PointD previousPointer, PointD pointer, RectD bounds, double width, double height)
        {
            return Clamp(new PointD(current.X + pointer.X - previousPointer.X, current.Y + pointer.Y - previousPointer.Y), width, height, bounds);
        }
        public static PointD Offset(PointD cardPosition, RectD host, double cardWidth, double scale, double top, double right)
        {
            return new PointD((cardPosition.X - (host.Right - cardWidth - right * scale)) / scale,
                (cardPosition.Y - host.Top - top * scale) / scale);
        }
    }

    internal static class Native
    {
        [StructLayout(LayoutKind.Sequential)] internal struct RECT { public int Left, Top, Right, Bottom; }
        [StructLayout(LayoutKind.Sequential)] internal struct POINT { public int X, Y; }
        [DllImport("user32.dll")] internal static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
        [DllImport("user32.dll")] internal static extern bool GetCursorPos(out POINT point);
        [DllImport("user32.dll")] internal static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int width, int height, uint flags);
        [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] private static extern IntPtr GetLong64(IntPtr hwnd, int index);
        [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")] private static extern IntPtr SetLong64(IntPtr hwnd, int index, IntPtr value);
        [DllImport("user32.dll", EntryPoint = "GetWindowLongW")] private static extern int GetLong32(IntPtr hwnd, int index);
        [DllImport("user32.dll", EntryPoint = "SetWindowLongW")] private static extern int SetLong32(IntPtr hwnd, int index, int value);
        [DllImport("user32.dll")] internal static extern uint GetDpiForWindow(IntPtr hwnd);
        [DllImport("dwmapi.dll")] private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref uint value, int size);
        private static long GetStyle(IntPtr hwnd, int index) { return IntPtr.Size == 8 ? GetLong64(hwnd, index).ToInt64() : GetLong32(hwnd, index); }
        private static void SetStyle(IntPtr hwnd, int index, long value)
        { if (IntPtr.Size == 8) SetLong64(hwnd, index, new IntPtr(value)); else SetLong32(hwnd, index, (int)value); }
        internal static void Configure(IntPtr hwnd, bool preview)
        {
            long style = GetStyle(hwnd, -16) & ~(0x00800000L | 0x00400000L | 0x00040000L);
            long extended = GetStyle(hwnd, -20) & ~(0x100L | 0x200L | 0x20000L | 1L | 0x80L | 0x08000000L | 0x40000L);
            extended |= preview ? 0x40000L : 0x80L | 0x08000000L;
            SetStyle(hwnd, -16, style); SetStyle(hwnd, -20, extended);
            try
            {
                uint border = 0xFFFFFFFE, noCorners = 1, noFrame = 1;
                DwmSetWindowAttribute(hwnd, 34, ref border, 4);
                DwmSetWindowAttribute(hwnd, 33, ref noCorners, 4);
                DwmSetWindowAttribute(hwnd, 2, ref noFrame, 4);
            }
            catch (DllNotFoundException) { }
            SetWindowPos(hwnd, IntPtr.Zero, 0, 0, 0, 0, 0x0037);
        }
        internal static PointD Cursor()
        { POINT point; return GetCursorPos(out point) ? new PointD(point.X, point.Y) : new PointD(); }
    }

    internal static class JsonData
    {
        internal static object Read(string path)
        {
            using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            using (StreamReader reader = new StreamReader(stream, Encoding.UTF8, true))
            {
                if (stream.Length > 2097152) throw new FormatException("Snapshot too large");
                return new JavaScriptSerializer { MaxJsonLength = 2097152, RecursionLimit = 64 }.DeserializeObject(reader.ReadToEnd());
            }
        }
        internal static Dictionary<string, object> Map(object value)
        {
            Dictionary<string, object> map = value as Dictionary<string, object>;
            if (map == null) throw new FormatException("Expected object");
            return map;
        }
        internal static object Get(IDictionary<string, object> map, string key)
        { object value; return map.TryGetValue(key, out value) ? value : null; }
        internal static double Number(IDictionary<string, object> map, string key, double fallback)
        {
            object value = Get(map, key);
            if (value == null) return fallback;
            if (!(value is int || value is long || value is double || value is decimal)) throw new FormatException("Invalid number");
            double number = Convert.ToDouble(value, CultureInfo.InvariantCulture);
            if (Double.IsInfinity(number) || Double.IsNaN(number)) throw new FormatException("Invalid number");
            return number;
        }
        internal static string Text(IDictionary<string, object> map, string key, string fallback)
        { object value = Get(map, key); return value == null ? fallback : value as string ?? fallback; }
        internal static bool Flag(IDictionary<string, object> map, string key)
        { object value = Get(map, key); return value is bool && (bool)value; }
        internal static List<TaskNode> Tasks(object value, int depth, ref int count)
        {
            object[] array = value as object[];
            if (array == null || depth > 6 && array.Length > 0) throw new FormatException("Invalid plan");
            List<TaskNode> tasks = new List<TaskNode>();
            foreach (object item in array)
            {
                if (++count > 100) throw new FormatException("Too many tasks");
                Dictionary<string, object> data = Map(item);
                TaskNode node = new TaskNode { Step = Text(data, "step", ""), Status = Text(data, "status", "") };
                object children = Get(data, "children");
                if (children != null) node.Children = Tasks(children, depth + 1, ref count);
                tasks.Add(node);
            }
            return tasks;
        }
        internal static double Now { get { return (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds; } }
    }

    internal sealed class CachedRow
    {
        internal TaskNode Node;
        internal FormattedText Text, Disclosure;
        internal double Indent, NaturalHeight, Completion = 1;
        internal string Status;
    }

    internal sealed class ProgressSurface : FrameworkElement
    {
        private readonly ProgressWindow window;
        private readonly ViewMemory memory = new ViewMemory();
        private readonly Dictionary<string, CachedRow> rows = new Dictionary<string, CachedRow>();
        private readonly Dictionary<string, double> heights = new Dictionary<string, double>();
        private readonly Dictionary<string, MotionState> expansions = new Dictionary<string, MotionState>();
        private readonly Typeface face = new Typeface(new FontFamily("Segoe UI, Microsoft YaHei UI"), FontStyles.Normal, FontWeights.Normal, FontStretches.Normal);
        private readonly Brush ink = BrushColor(59, 62, 66), muted = BrushColor(137, 141, 146), pale = BrushColor(229, 232, 234);
        private readonly Brush green = BrushColor(58, 130, 93), amber = BrushColor(170, 130, 63);
        private readonly Brush softGreen = BrushColor(232, 245, 237), hoverBrush = BrushColor(245, 246, 247);
        private readonly Brush shadowOuter = BrushColor(0, 0, 0, 5), shadowInner = BrushColor(0, 0, 0, 7);
        private List<TaskNode> plan = new List<TaskNode>();
        private ChatViewState view = new ChatViewState();
        private LayoutResult layout = new LayoutResult();
        private MotionState height = new MotionState(36, 0), progress = new MotionState();
        private FormattedText heading, titleText, countText, footerText;
        private string chat = "", title = "", runState = "idle", tooltipText = "";
        private double width = 300, targetProgress, phase, scrollPointer, scrollStart;
        private bool foldMotion, scrollDragging;
        private Point hover = new Point(-100, -100);
        private const double Pad = 6, BodyTop = 66, Footer = 20;
        internal ProgressSurface(ProgressWindow window)
        {
            this.window = window; Focusable = false; ClipToBounds = true;
            SnapsToDevicePixels = false;
            TextOptions.SetTextFormattingMode(this, TextFormattingMode.Display);
            ToolTipService.SetInitialShowDelay(this, 550); ToolTipService.SetShowDuration(this, 12000);
            heading = Text("任务进度", 12, muted);
        }
        private static SolidColorBrush BrushColor(byte r, byte g, byte b, byte a = 255)
        { SolidColorBrush brush = new SolidColorBrush(Color.FromArgb(a, r, g, b)); brush.Freeze(); return brush; }
        private FormattedText Text(string text, double size, Brush brush)
        { return new FormattedText(text, CultureInfo.GetCultureInfo("zh-CN"), FlowDirection.LeftToRight, face, size, brush); }
        internal double CardHeight { get { return Math.Max(36, Math.Min(256, height.Value)); } }
        private double FullHeight { get { return Math.Min(256, Math.Max(106, BodyTop + layout.Height + Footer)); } }
        private double ViewportHeight { get { return Math.Max(1, FullHeight - BodyTop - Footer); } }
        private double ScrollLimit { get { return Math.Max(0, layout.Height - ViewportHeight); } }
        internal bool HasAnimation
        {
            get
            {
                if (foldMotion || Math.Abs(progress.Value - targetProgress) > .0001) return true;
                foreach (TaskNode node in PlanModel.Walk(plan))
                {
                    MotionState motion;
                    if (node.Children.Count > 0 && expansions.TryGetValue(node.Key, out motion) &&
                        (Math.Abs(motion.Value - (view.Expanded.Contains(node.Key) ? 1 : 0)) > .00001 || Math.Abs(motion.Velocity) > .00001)) return true;
                }
                foreach (RowLayout row in layout.Rows)
                {
                    CachedRow cached;
                    if (IsRowVisible(row) && rows.TryGetValue(row.Key, out cached) &&
                        (runState == "running" && (cached.Status == "in_progress" || cached.Status == "blocked") || cached.Completion < 1)) return true;
                }
                return false;
            }
        }
        private bool IsRowVisible(RowLayout row)
        {
            return CardHeight > BodyTop && row.ClipHeight > 0 && row.Top + row.ClipHeight > view.Scroll &&
                row.Top < view.Scroll + Math.Min(ViewportHeight, CardHeight - BodyTop);
        }
        internal void UpdateState(string nextChat, string nextTitle, string nextRunState, List<TaskNode> nextPlan, double nextWidth)
        {
            bool changedChat = chat != nextChat, changedWidth = Math.Abs(width - nextWidth) > .01;
            if (changedChat)
            {
                Suspend(); rows.Clear(); expansions.Clear(); heights.Clear();
                chat = nextChat; view = memory.Get(chat); foldMotion = false;
            }
            title = nextTitle; runState = nextRunState; plan = nextPlan; width = nextWidth;
            HashSet<string> keys = new HashSet<string>(PlanModel.Walk(plan).Select(delegate(TaskNode n) { return n.Key; }));
            foreach (string old in rows.Keys.Where(delegate(string key) { return !keys.Contains(key); }).ToArray()) { rows.Remove(old); heights.Remove(old); expansions.Remove(old); }
            view.Expanded.RemoveWhere(delegate(string key) { return !keys.Contains(key); });
            Action<IList<TaskNode>, int> cache = null;
            cache = delegate(IList<TaskNode> nodes, int depth)
            {
                foreach (TaskNode node in nodes)
                {
                    CachedRow row; bool existed = rows.TryGetValue(node.Key, out row);
                    if (!existed) rows[node.Key] = row = new CachedRow();
                    bool changedDisclosure = existed && (row.Node.Children.Count > 0) != (node.Children.Count > 0);
                    if (existed && row.Status != "completed" && node.Status == "completed") row.Completion = 0;
                    if (node.Status != "completed") row.Completion = 1;
                    row.Node = node; row.Status = node.Status; row.Indent = depth * 12;
                    if (!existed || changedWidth || changedDisclosure)
                    {
                        row.Text = Text(node.Step, 12, ink);
                        row.Text.MaxTextWidth = Math.Max(80, width - 44 - row.Indent - (node.Children.Count > 0 ? 43 : 13));
                        row.Text.MaxTextHeight = 36; row.Text.Trimming = TextTrimming.CharacterEllipsis;
                        row.NaturalHeight = Math.Max(22, row.Text.Height + 6);
                    }
                    row.Text.SetForegroundBrush(node.Status == "completed" ? muted : ink);
                    heights[node.Key] = row.NaturalHeight;
                    if (node.Children.Count > 0)
                    {
                        Counts count = PlanModel.Count(node.Children);
                        row.Disclosure = Text(count.Completed + "/" + count.Total, 9.5, muted);
                        if (!expansions.ContainsKey(node.Key)) expansions[node.Key] = new MotionState(view.Expanded.Contains(node.Key) ? 1 : 0, 0);
                    }
                    cache(node.Children, depth + 1);
                }
            };
            cache(plan, 0);
            Counts total = PlanModel.Count(plan);
            targetProgress = total.Total == 0 ? 0 : (double)total.Completed / total.Total;
            if (changedChat) progress = new MotionState(targetProgress, 0);
            titleText = Text(title, 10.5, muted); titleText.MaxTextWidth = Math.Max(80, width - 28); titleText.MaxTextHeight = 15; titleText.Trimming = TextTrimming.CharacterEllipsis;
            countText = Text(total.Completed + "/" + total.Total, 10.5, muted);
            bool working = PlanModel.Walk(plan).Any(delegate(TaskNode node) { return node.Status == "in_progress"; });
            bool blocked = PlanModel.Walk(plan).Any(delegate(TaskNode node) { return node.Status == "blocked"; });
            footerText = Text(total.Total > 0 && total.Completed == total.Total ? "全部完成" : runState == "interrupted" ? "已暂停" :
                runState == "running" && working ? "正在执行" : blocked ? "等待处理" : "等待继续", 9.5, muted);
            Reflow();
            if (changedChat || !foldMotion) height = new MotionState(view.Collapsed ? 36 : FullHeight, 0);
            InvalidateVisual(); window.WakeAnimation();
        }
        internal void RebuildText()
        {
            if (plan.Count == 0) return;
            foreach (CachedRow row in rows.Values) row.Text = null;
            rows.Clear(); UpdateState(chat, title, runState, plan, width);
        }
        private void Reflow()
        {
            layout = PlanLayout.Build(plan, heights, expansions);
            view.Scroll = Math.Max(0, Math.Min(ScrollLimit, view.Scroll));
        }
        internal bool Advance(double dt)
        {
            bool branchesChanged = false;
            foreach (TaskNode node in PlanModel.Walk(plan))
            {
                if (node.Children.Count == 0) continue;
                MotionState old = expansions[node.Key]; double target = view.Expanded.Contains(node.Key) ? 1 : 0;
                if (old.Value == target && old.Velocity == 0) continue;
                MotionState next = Motion.Sample(old.Value, old.Velocity, target, dt);
                if (next.Value < 0 || next.Value > 1) { next.Value = Math.Max(0, Math.Min(1, next.Value)); next.Velocity = 0; }
                double extent; if (!layout.ChildExtents.TryGetValue(node.Key, out extent)) extent = 24;
                if (Math.Abs(next.Value - target) * Math.Max(24, extent) < .25 && Math.Abs(next.Velocity) * Math.Max(24, extent) < 5)
                    next = new MotionState(target, 0);
                expansions[node.Key] = next; branchesChanged = true;
            }
            if (branchesChanged) Reflow();
            double desiredHeight = view.Collapsed ? 36 : FullHeight;
            if (foldMotion)
            {
                height = Motion.Sample(height.Value, height.Velocity, desiredHeight, dt);
                if (height.Value < 36 || height.Value > 256) { height.Value = Math.Max(36, Math.Min(256, height.Value)); height.Velocity = 0; }
                if (Math.Abs(height.Value - desiredHeight) < .25 && Math.Abs(height.Velocity) < 5)
                { height = new MotionState(desiredHeight, 0); foldMotion = false; }
            }
            else height = new MotionState(desiredHeight, 0);
            progress = Motion.Sample(progress.Value, progress.Velocity, targetProgress, dt);
            if (Math.Abs(progress.Value - targetProgress) < .0001 && Math.Abs(progress.Velocity) < .001) progress = new MotionState(targetProgress, 0);
            if (runState == "running") phase += dt;
            foreach (RowLayout row in layout.Rows)
            {
                CachedRow cached;
                if (IsRowVisible(row) && rows.TryGetValue(row.Key, out cached) && cached.Completion < 1)
                    cached.Completion = Math.Min(1, cached.Completion + dt / .38);
            }
            InvalidateVisual(); return HasAnimation;
        }
        internal void Suspend()
        {
            hover = new Point(-100, -100); ToolTip = null; tooltipText = "";
            scrollDragging = false;
            if (IsMouseCaptured) Mouse.Capture(null);
        }
        private Rect CardRect { get { return new Rect(Pad, Pad, width, CardHeight); } }
        private Rect FoldRect { get { return new Rect(width - 54, 7, 24, 24); } }
        private Rect CloseRect { get { return new Rect(width - 28, 7, 24, 24); } }
        private Rect ScrollRect
        {
            get
            {
                if (ScrollLimit <= 0) return Rect.Empty;
                double track = ViewportHeight - 4, thumb = Math.Max(20, track * ViewportHeight / Math.Max(1, layout.Height));
                return new Rect(width - 7, BodyTop + 2 + (track - thumb) * view.Scroll / ScrollLimit, 3, thumb);
            }
        }
        protected override void OnRender(DrawingContext dc)
        {
            base.OnRender(dc);
            if (plan.Count == 0) return;
            double cardHeight = CardHeight;
            // Actual alpha zero outside these shapes leaves the empty fixed host transparent to input.
            dc.DrawRoundedRectangle(shadowOuter, null, new Rect(Pad - 3, Pad, width + 6, cardHeight + 5), 18, 18);
            dc.DrawRoundedRectangle(shadowInner, null, new Rect(Pad - 1, Pad + 1, width + 2, cardHeight + 2), 17, 17);
            dc.DrawRoundedRectangle(Brushes.White, null, CardRect, 16, 16);
            dc.PushClip(new RectangleGeometry(CardRect, 16, 16));
            dc.PushTransform(new TranslateTransform(Pad, Pad));
            dc.DrawText(heading, new Point(13, 10));
            if (countText != null) dc.DrawText(countText, new Point(width - 65 - countText.Width, 11));
            DrawButton(dc, FoldRect, false); DrawButton(dc, CloseRect, true);
            if (cardHeight > 36)
            {
                dc.DrawLine(new Pen(BrushColor(240, 241, 242), .7), new Point(12, 36), new Point(width - 12, 36));
                if (titleText != null) dc.DrawText(titleText, new Point(13, 42));
                dc.DrawRoundedRectangle(BrushColor(242, 243, 244), null, new Rect(13, 60, width - 26, 2), 1, 1);
                if (progress.Value > 0) dc.DrawRoundedRectangle(green, null, new Rect(13, 60, (width - 26) * Math.Max(0, Math.Min(1, progress.Value)), 2), 1, 1);
                dc.PushClip(new RectangleGeometry(new Rect(10, BodyTop, width - 20, ViewportHeight)));
                foreach (RowLayout row in layout.Rows)
                {
                    if (!IsRowVisible(row)) continue;
                    CachedRow cached = rows[row.Key]; double y = BodyTop + row.Top - view.Scroll;
                    dc.PushClip(new RectangleGeometry(new Rect(10, y, width - 20, row.ClipHeight)));
                    if (cached.Node.Children.Count > 0 && hover.Y >= y && hover.Y < y + row.NaturalHeight && hover.X > 9 && hover.X < width - 9)
                        dc.DrawRoundedRectangle(hoverBrush, null, new Rect(10, y, width - 20, row.NaturalHeight), 5, 5);
                    DrawStatus(dc, cached, 22 + cached.Indent, y + 11);
                    dc.DrawText(cached.Text, new Point(36 + cached.Indent, y + 3));
                    if (cached.Node.Children.Count > 0)
                    {
                        dc.DrawText(cached.Disclosure, new Point(width - 24 - cached.Disclosure.Width, y + 5));
                        double angle = expansions[row.Key].Value * 90;
                        dc.PushTransform(new RotateTransform(angle, width - 16, y + 11));
                        Pen pen = new Pen(muted, 1.15) { StartLineCap = PenLineCap.Round, EndLineCap = PenLineCap.Round };
                        dc.DrawLine(pen, new Point(width - 18, y + 8), new Point(width - 15, y + 11));
                        dc.DrawLine(pen, new Point(width - 15, y + 11), new Point(width - 18, y + 14)); dc.Pop();
                    }
                    dc.Pop();
                }
                dc.Pop();
                Rect thumb = ScrollRect; if (!thumb.IsEmpty) dc.DrawRoundedRectangle(BrushColor(205, 209, 212), null, thumb, 1.5, 1.5);
                if (footerText != null) dc.DrawText(footerText, new Point(13, FullHeight - 15));
            }
            dc.Pop(); dc.Pop();
        }
        private void DrawButton(DrawingContext dc, Rect rect, bool close)
        {
            if (rect.Contains(hover)) dc.DrawRoundedRectangle(hoverBrush, null, rect, 6, 6);
            double x = rect.X + 12, y = rect.Y + 12;
            Pen pen = new Pen(muted, 1.15) { StartLineCap = PenLineCap.Round, EndLineCap = PenLineCap.Round };
            if (close)
            {
                dc.DrawLine(pen, new Point(x - 3, y - 3), new Point(x + 3, y + 3));
                dc.DrawLine(pen, new Point(x + 3, y - 3), new Point(x - 3, y + 3));
            }
            else
            {
                double direction = view.Collapsed ? 1 : -1;
                dc.DrawLine(pen, new Point(x - 3, y - direction), new Point(x, y + 2 * direction));
                dc.DrawLine(pen, new Point(x, y + 2 * direction), new Point(x + 3, y - direction));
            }
        }
        private void DrawStatus(DrawingContext dc, CachedRow row, double x, double y)
        {
            if (row.Status == "pending") { dc.DrawEllipse(null, new Pen(pale, 1.25), new Point(x, y), 4.5, 4.5); return; }
            if (row.Status == "completed")
            {
                double reveal = 1 - Math.Pow(1 - row.Completion, 3);
                dc.DrawEllipse(softGreen, null, new Point(x, y), 7, 7);
                Point a = new Point(x - 3.2, y), b = new Point(x - .7, y + 2.3), c = new Point(x + 3.8, y - 2.6);
                Pen pen = new Pen(green, 1.35) { StartLineCap = PenLineCap.Round, EndLineCap = PenLineCap.Round, LineJoin = PenLineJoin.Round };
                double first = Math.Min(1, reveal * 3);
                dc.DrawLine(pen, a, new Point(a.X + (b.X - a.X) * first, a.Y + (b.Y - a.Y) * first));
                if (reveal > 1.0 / 3) { double second = (reveal - 1.0 / 3) * 1.5; dc.DrawLine(pen, b, new Point(b.X + (c.X - b.X) * second, b.Y + (c.Y - b.Y) * second)); }
                return;
            }
            if (row.Status == "blocked")
            {
                dc.PushOpacity(.7 + .2 * Math.Sin(phase * 2.2));
                dc.DrawEllipse(null, new Pen(amber, 1.15), new Point(x, y), 6, 6);
                dc.DrawLine(new Pen(amber, 1.25), new Point(x, y - 2.6), new Point(x, y + .5));
                dc.DrawEllipse(amber, null, new Point(x, y + 3), .7, .7); dc.Pop(); return;
            }
            dc.DrawEllipse(null, new Pen(pale, 1), new Point(x, y), 6, 6);
            double angle = phase * 3.7, end = angle + 4.3;
            Point startPoint = new Point(x + Math.Cos(angle) * 6, y + Math.Sin(angle) * 6);
            Point endPoint = new Point(x + Math.Cos(end) * 6, y + Math.Sin(end) * 6);
            StreamGeometry arc = new StreamGeometry();
            using (StreamGeometryContext geometry = arc.Open())
            { geometry.BeginFigure(startPoint, false, false); geometry.ArcTo(endPoint, new Size(6, 6), 0, true, SweepDirection.Clockwise, true, false); }
            dc.DrawGeometry(null, new Pen(muted, 1.2) { StartLineCap = PenLineCap.Round, EndLineCap = PenLineCap.Round }, arc);
            dc.PushOpacity(.45 + .15 * Math.Sin(phase * 2.5)); dc.DrawEllipse(muted, null, new Point(x, y), 1.6, 1.6); dc.Pop();
        }
        private RowLayout HitRow(Point point)
        {
            if (point.Y < BodyTop || point.Y > Math.Min(CardHeight, BodyTop + ViewportHeight)) return null;
            double y = point.Y - BodyTop + view.Scroll;
            return layout.Rows.FirstOrDefault(delegate(RowLayout row) { return row.ClipHeight > 0 && y >= row.Top && y < row.Top + row.ClipHeight; });
        }
        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e); Point p = e.GetPosition(this); hover = new Point(p.X - Pad, p.Y - Pad);
            if (scrollDragging)
            {
                Rect thumb = ScrollRect; double travel = ViewportHeight - 4 - thumb.Height;
                if (travel > 0) view.Scroll = Math.Max(0, Math.Min(ScrollLimit, scrollStart + (p.Y - scrollPointer) / travel * ScrollLimit));
            }
            else window.Drag();
            RowLayout row = HitRow(hover);
            string nextTooltip = FoldRect.Contains(hover) ? (view.Collapsed ? "展开任务" : "收起任务") : CloseRect.Contains(hover) ? "关闭进度窗" :
                hover.Y >= 58 && hover.Y <= 64 ? Math.Round(targetProgress * 100) + "%" : row != null ? row.Node.Step : hover.Y >= 38 && hover.Y <= 57 ? title : "";
            if (tooltipText != nextTooltip) { ToolTip = String.IsNullOrEmpty(nextTooltip) ? null : nextTooltip; tooltipText = nextTooltip; }
            Cursor = FoldRect.Contains(hover) || CloseRect.Contains(hover) || row != null && row.Node.Children.Count > 0 ? Cursors.Hand : Cursors.Arrow;
            InvalidateVisual(); if (HasAnimation) window.WakeAnimation();
        }
        protected override void OnMouseLeave(MouseEventArgs e)
        { base.OnMouseLeave(e); hover = new Point(-100, -100); InvalidateVisual(); }
        protected override void OnMouseLeftButtonDown(MouseButtonEventArgs e)
        {
            base.OnMouseLeftButtonDown(e); Point p = e.GetPosition(this); p = new Point(p.X - Pad, p.Y - Pad);
            if (CloseRect.Contains(p)) { window.Close(); e.Handled = true; return; }
            if (FoldRect.Contains(p))
            { view.Collapsed = !view.Collapsed; foldMotion = true; ToolTip = null; window.WakeAnimation(); e.Handled = true; return; }
            Rect thumb = ScrollRect; if (!thumb.IsEmpty) thumb.Inflate(4, 2);
            if (!thumb.IsEmpty && thumb.Contains(p))
            { scrollDragging = true; scrollPointer = e.GetPosition(this).Y; scrollStart = view.Scroll; Mouse.Capture(this); e.Handled = true; return; }
            RowLayout row = HitRow(p);
            if (row != null && row.Node.Children.Count > 0)
            {
                if (!view.Expanded.Remove(row.Key)) view.Expanded.Add(row.Key);
                ToolTip = null; window.WakeAnimation(); e.Handled = true; return;
            }
            if (p.X >= 0 && p.X <= width && p.Y >= 0 && p.Y <= CardHeight)
            {
                Point pressScreen = PointToScreen(e.GetPosition(this));
                ToolTip = null; window.BeginDrag(new PointD(pressScreen.X, pressScreen.Y)); e.Handled = true;
            }
        }
        protected override void OnMouseLeftButtonUp(MouseButtonEventArgs e)
        {
            base.OnMouseLeftButtonUp(e);
            if (scrollDragging) { scrollDragging = false; Mouse.Capture(null); }
            else window.EndDrag(true);
        }
        protected override void OnLostMouseCapture(MouseEventArgs e)
        {
            base.OnLostMouseCapture(e);
            scrollDragging = false; window.EndDrag(false);
        }
        protected override void OnMouseWheel(MouseWheelEventArgs e)
        {
            base.OnMouseWheel(e);
            if (view.Collapsed || ScrollLimit <= 0) return;
            view.Scroll = Math.Max(0, Math.Min(ScrollLimit, view.Scroll - e.Delta / 120.0 * 34));
            InvalidateVisual(); if (HasAnimation) window.WakeAnimation(); e.Handled = true;
        }
    }

    public sealed class ProgressWindow : Window
    {
        private readonly string viewFile, dataDir, diagnostics;
        private readonly bool preview;
        private readonly Process parent;
        private readonly DispatcherTimer poll;
        private readonly ProgressSurface surface;
        private readonly Stopwatch clock = Stopwatch.StartNew();
        private readonly List<double> intervals = new List<double>(), costs = new List<double>();
        private IntPtr handle;
        private HwndSource source;
        private RectD host;
        private Dictionary<string, object> placement = new Dictionary<string, object>();
        private long revision = -1;
        private double heartbeat, lastFrame, lastLocalSave;
        private TimeSpan lastRender = TimeSpan.MinValue;
        private int reading, sizeChanges;
        private bool closing, rendering, dragging, allowedVisible;
        private PointD lastPointer;
        private double scale = 1, cardWidth = 300;
        public ProgressWindow(string viewFile, string dataDir, int parentPid, bool preview, string diagnostics)
        {
            this.viewFile = viewFile; this.dataDir = dataDir; this.preview = preview; this.diagnostics = diagnostics;
            if (parentPid > 0) parent = Process.GetProcessById(parentPid);
            Title = "Codex · 任务进度";
            WindowStyle = WindowStyle.None; AllowsTransparency = true; Background = Brushes.Transparent;
            ResizeMode = ResizeMode.NoResize; ShowInTaskbar = preview; ShowActivated = false; Topmost = true;
            Width = cardWidth + 12; Height = 268; WindowStartupLocation = WindowStartupLocation.Manual;
            surface = new ProgressSurface(this); Content = surface;
            SourceInitialized += delegate
            {
                handle = new WindowInteropHelper(this).Handle;
                source = HwndSource.FromHwnd(handle); source.AddHook(WindowMessage);
                Native.Configure(handle, preview); UpdateScale(); Position();
            };
            Closed += OnClosed;
            poll = new DispatcherTimer(DispatcherPriority.Background) { Interval = TimeSpan.FromMilliseconds(100) };
            poll.Tick += Poll; poll.Start();
        }
        internal double Scale { get { return scale; } }
        internal void WakeAnimation()
        {
            if (!IsVisible || rendering || closing) return;
            lastFrame = clock.Elapsed.TotalSeconds; lastRender = TimeSpan.MinValue;
            CompositionTarget.Rendering += Frame; rendering = true;
        }
        private void StopAnimation()
        {
            if (rendering) CompositionTarget.Rendering -= Frame;
            rendering = false; lastFrame = 0; lastRender = TimeSpan.MinValue;
        }
        private void Frame(object sender, EventArgs args)
        {
            RenderingEventArgs renderingArgs = args as RenderingEventArgs;
            if (renderingArgs != null && renderingArgs.RenderingTime == lastRender) return;
            if (renderingArgs != null) lastRender = renderingArgs.RenderingTime;
            double start = clock.Elapsed.TotalSeconds;
            double dt = Math.Max(0, start - lastFrame); lastFrame = start;
            if (!String.IsNullOrEmpty(diagnostics) && dt > 0 && intervals.Count < 100000) intervals.Add(dt * 1000);
            bool more = surface.Advance(Math.Min(.1, dt));
            if (!String.IsNullOrEmpty(diagnostics) && costs.Count < 100000) costs.Add((clock.Elapsed.TotalSeconds - start) * 1000);
            if (!more) StopAnimation();
        }
        private IntPtr WindowMessage(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam, ref bool handled)
        {
            if (message == 0x0021 && !preview) { handled = true; return new IntPtr(3); }
            if (message == 0x0005) sizeChanges++;
            if (message == 0x02E0) Dispatcher.BeginInvoke(new Action(delegate { UpdateScale(); surface.RebuildText(); Position(); }));
            return IntPtr.Zero;
        }
        private void UpdateScale()
        {
            if (source != null && source.CompositionTarget != null) scale = source.CompositionTarget.TransformToDevice.M11;
            else if (handle != IntPtr.Zero)
            {
                try { scale = Native.GetDpiForWindow(handle) / 96.0; } catch (EntryPointNotFoundException) { }
            }
            if (scale <= 0) scale = 1;
        }
        private void Poll(object sender, EventArgs args)
        {
            if (closing) return;
            try { if (parent != null && parent.HasExited) { Close(); return; } }
            catch (InvalidOperationException) { Close(); return; }
            if (JsonData.Now - heartbeat > 2) SetVisible(false);
            if (Interlocked.CompareExchange(ref reading, 1, 0) != 0) return;
            Task.Run(delegate
            {
                Dictionary<string, object> snapshot = null; bool malformed = false;
                try { snapshot = JsonData.Map(JsonData.Read(viewFile)); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
                catch (Exception) { malformed = true; }
                if (closing) { Interlocked.Exchange(ref reading, 0); return; }
                Dispatcher.BeginInvoke(new Action(delegate
                {
                    Interlocked.Exchange(ref reading, 0);
                    if (closing) return;
                    if (malformed) { SetVisible(false); return; }
                    if (snapshot == null) return;
                    try { Apply(snapshot); }
                    catch (Exception) { allowedVisible = false; SetVisible(false); }
                }));
            });
        }
        private void Apply(Dictionary<string, object> snapshot)
        {
            if (JsonData.Flag(snapshot, "terminate")) { Close(); return; }
            if (JsonData.Number(snapshot, "schema", 0) != 1) throw new FormatException("Unknown schema");
            double timestamp = JsonData.Number(snapshot, "emitted_at", 0);
            if (JsonData.Now - timestamp > 2 || timestamp > JsonData.Now + 2) { SetVisible(false); return; }
            heartbeat = timestamp;
            double revisionNumber = JsonData.Number(snapshot, "revision", -1);
            if (revisionNumber < 0 || revisionNumber != Math.Floor(revisionNumber)) throw new FormatException("Invalid revision");
            long nextRevision = (long)revisionNumber;
            if (nextRevision != revision)
            {
                bool requested = JsonData.Flag(snapshot, "visible");
                if (!requested) { revision = nextRevision; allowedVisible = false; SetVisible(false); return; }
                object[] rect = JsonData.Get(snapshot, "host_rect") as object[];
                if (rect == null || rect.Length != 4) throw new FormatException("Invalid host");
                double[] coordinates = rect.Select(delegate(object v) { return Convert.ToDouble(v, CultureInfo.InvariantCulture); }).ToArray();
                if (coordinates.Any(delegate(double v) { return Double.IsNaN(v) || Double.IsInfinity(v) || Math.Abs(v) > 100000; }) ||
                    coordinates[2] <= coordinates[0] || coordinates[3] <= coordinates[1]) throw new FormatException("Invalid host");
                Dictionary<string, object> state = JsonData.Map(JsonData.Get(snapshot, "state"));
                int count = 0;
                List<TaskNode> plan = JsonData.Tasks(JsonData.Get(state, "plan"), 1, ref count);
                PlanModel.ValidateAndKey(plan);
                string thread = JsonData.Text(state, "thread_id", "");
                if (String.IsNullOrWhiteSpace(thread) || thread.Length > 200) throw new FormatException("Invalid chat");
                Dictionary<string, object> nextPlacement = JsonData.Map(JsonData.Get(snapshot, "placement"));
                host = new RectD(coordinates[0], coordinates[1], coordinates[2], coordinates[3]);
                if (!dragging && timestamp >= lastLocalSave) placement = nextPlacement;
                double width = Math.Max(220, Math.Min(480, JsonData.Number(placement, "width", 300)));
                if (Math.Abs(width - cardWidth) > .01) { cardWidth = width; Width = cardWidth + 12; }
                surface.UpdateState(thread, JsonData.Text(state, "title", "当前任务"), JsonData.Text(state, "run_state", "idle"), plan, cardWidth);
                allowedVisible = plan.Count > 0; revision = nextRevision;
                if (!dragging) Position();
            }
            SetVisible(allowedVisible);
        }
        private void SetVisible(bool visible)
        {
            if (!visible)
            {
                if (dragging) EndDrag(false);
                StopAnimation(); surface.Suspend(); if (IsVisible) Hide();
            }
            else
            {
                if (!IsVisible) { Show(); UpdateScale(); Position(); }
                surface.InvalidateVisual(); if (surface.HasAnimation) WakeAnimation();
            }
        }
        private void Position()
        {
            if (host == null || handle == IntPtr.Zero || dragging) return;
            double top = JsonData.Number(placement, "top", 432), right = JsonData.Number(placement, "right", 12);
            PointD point = DragMath.Clamp(new PointD(host.Right - cardWidth * scale - right * scale + JsonData.Number(placement, "offset_x", 0) * scale,
                host.Top + top * scale + JsonData.Number(placement, "offset_y", 0) * scale), cardWidth * scale, surface.CardHeight * scale, host);
            MoveCard(point);
        }
        private void MoveCard(PointD point)
        {
            int x = (int)Math.Round(point.X - 6 * scale), y = (int)Math.Round(point.Y - 6 * scale);
            Native.RECT current;
            if (Native.GetWindowRect(handle, out current) && current.Left == x && current.Top == y) return;
            Native.SetWindowPos(handle, IntPtr.Zero, x, y, 0, 0, 0x0015);
        }
        internal void BeginDrag(PointD pressScreen)
        {
            if (handle == IntPtr.Zero || host == null) return;
            dragging = true; lastPointer = pressScreen; Mouse.Capture(surface);
        }
        internal void Drag()
        {
            if (!dragging) return;
            PointD pointer = Native.Cursor(); Native.RECT current;
            if (Native.GetWindowRect(handle, out current))
                MoveCard(DragMath.Move(new PointD(current.Left + 6 * scale, current.Top + 6 * scale), lastPointer, pointer,
                    host, cardWidth * scale, surface.CardHeight * scale));
            lastPointer = pointer;
        }
        internal void EndDrag(bool applyRelease)
        {
            if (!dragging) return;
            if (applyRelease) Drag(); dragging = false; Mouse.Capture(null);
            Native.RECT current;
            if (host == null || !Native.GetWindowRect(handle, out current)) return;
            PointD offset = DragMath.Offset(new PointD(current.Left + 6 * scale, current.Top + 6 * scale), host, cardWidth * scale, scale,
                JsonData.Number(placement, "top", 432), JsonData.Number(placement, "right", 12));
            placement["offset_x"] = offset.X; placement["offset_y"] = offset.Y; lastLocalSave = JsonData.Now;
            SavePlacement(offset, 0);
        }
        private void SavePlacement(PointD offset, int attempt)
        {
            string path = Path.Combine(dataDir, "placement.json"), temporary = path + ".wpf." + Process.GetCurrentProcess().Id + ".tmp";
            try
            {
                Dictionary<string, object> saved;
                try { saved = JsonData.Map(JsonData.Read(path)); } catch (Exception) { saved = new Dictionary<string, object>(placement); }
                saved["offset_x"] = offset.X; saved["offset_y"] = offset.Y;
                Directory.CreateDirectory(dataDir);
                File.WriteAllText(temporary, new JavaScriptSerializer().Serialize(saved), new UTF8Encoding(false));
                if (File.Exists(path)) File.Replace(temporary, path, null); else File.Move(temporary, path);
            }
            catch (IOException)
            {
                if (attempt < 2 && !closing)
                {
                    DispatcherTimer retry = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(100) };
                    retry.Tick += delegate { retry.Stop(); if (!closing) SavePlacement(offset, attempt + 1); }; retry.Start();
                }
            }
            catch (UnauthorizedAccessException) { }
            finally { try { if (File.Exists(temporary)) File.Delete(temporary); } catch (IOException) { } catch (UnauthorizedAccessException) { } }
        }
        private void OnClosed(object sender, EventArgs args)
        {
            closing = true; poll.Stop(); StopAnimation(); surface.Suspend();
            if (parent != null) parent.Dispose();
            if (!String.IsNullOrEmpty(diagnostics))
            {
                try
                {
                    File.WriteAllText(diagnostics, new JavaScriptSerializer { MaxJsonLength = 8388608 }.Serialize(new {
                        renderer = "wpf", render_tier = RenderCapability.Tier >> 16, dpi = scale * 96,
                        host_size_changes = sizeChanges, rendering_callback_intervals_ms = intervals,
                        rendering_handler_cost_ms = costs, metric_note = "Rendering callbacks are not presented-frame measurements."
                    }), new UTF8Encoding(false));
                }
                catch (Exception) { }
            }
            Application.Current.Shutdown();
        }
    }

    public static class Program
    {
        [STAThread]
        public static void Main(string[] args)
        {
            string viewFile = null, dataDir = null, diagnostics = null; int parentPid = 0; bool preview = false;
            for (int i = 0; i < args.Length; i++)
            {
                if (args[i] == "--preview") preview = true;
                else if (args[i] == "--view-file" && i + 1 < args.Length) viewFile = args[++i];
                else if (args[i] == "--data-dir" && i + 1 < args.Length) dataDir = args[++i];
                else if (args[i] == "--parent-pid" && i + 1 < args.Length) Int32.TryParse(args[++i], out parentPid);
                else if (args[i] == "--diagnostics" && i + 1 < args.Length) diagnostics = args[++i];
            }
            if (String.IsNullOrEmpty(viewFile) || String.IsNullOrEmpty(dataDir)) return;
            try
            {
                Application app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
                new ProgressWindow(Path.GetFullPath(viewFile), Path.GetFullPath(dataDir), parentPid, preview, diagnostics);
                app.Run();
            }
            catch (ArgumentException) { }
            catch (InvalidOperationException) { }
        }
    }
}
