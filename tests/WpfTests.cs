using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Windows.Interop;
using CodexPlanProgress;

public static class WpfTests
{
    private static void Check(bool condition, string message)
    {
        if (!condition) throw new Exception(message);
    }

    private static void Near(double actual, double expected, string message, double tolerance = 0.000001)
    {
        Check(Math.Abs(actual - expected) <= tolerance,
              message + ": expected " + expected + ", got " + actual);
    }

    private static TaskNode Node(string step, string status, params TaskNode[] children)
    {
        return new TaskNode { Step = step, Status = status, Children = new List<TaskNode>(children) };
    }

    private static void Reject(Action action, string message)
    {
        try { action(); }
        catch (Exception) { return; }
        throw new Exception(message);
    }

    private static List<TaskNode> Descendants(TaskNode parent)
    {
        var result = new List<TaskNode>();
        foreach (var child in parent.Children)
        {
            result.Add(child);
            result.AddRange(Descendants(child));
        }
        return result;
    }

    private static Dictionary<string, double> Heights(IList<TaskNode> plan)
    {
        var result = new Dictionary<string, double>();
        foreach (var root in plan)
        {
            result[root.Key] = 28;
            int index = 0;
            foreach (var node in Descendants(root)) result[node.Key] = ++index % 3 == 0 ? 54 : 32;
        }
        return result;
    }

    private static void OneBoundary(IList<RowLayout> rows)
    {
        int partial = 0;
        bool ended = false;
        foreach (var row in rows)
        {
            Check(row.NaturalHeight > 0, "Natural text height must remain positive");
            Check(row.ClipHeight >= -0.000001 && row.ClipHeight <= row.NaturalHeight + 0.000001,
                  "A row must be clipped within its natural height");
            if (row.ClipHeight > 0.000001)
            {
                Check(!ended, "Rows must reveal one contiguous prefix");
                if (row.ClipHeight < row.NaturalHeight - 0.000001) { partial++; ended = true; }
            }
            else ended = true;
        }
        Check(partial <= 1, "One expanding group must not expose strips from multiple rows");
    }

    private static void MotionContinuity()
    {
        var forward = Motion.Sample(0, 0, 1, 0.05);
        Check(forward.Value > 0 && forward.Value < 1 && forward.Velocity > 0, "Opening must have an intermediate state");
        var reverse = Motion.Sample(forward.Value, forward.Velocity, 0, 0);
        Near(reverse.Value, forward.Value, "Retargeting must preserve position");
        Near(reverse.Velocity, forward.Velocity, "Retargeting must preserve velocity");
        var early = Motion.Sample(reverse.Value, reverse.Velocity, 0, 0.004);
        Check(early.Velocity > 0 && early.Velocity < reverse.Velocity, "Reversal must decelerate before changing direction");
        var later = Motion.Sample(early.Value, early.Velocity, 0, 0.03);
        Check(later.Velocity < 0, "Reversal must eventually move towards the new target");
        var settled = Motion.Sample(later.Value, later.Velocity, 0, 0.5);
        Near(settled.Value, 0, "Closing must converge", 0.001);
        Check(Math.Abs(settled.Velocity) < 0.01, "Settled velocity must decay");
        var a = Motion.Sample(0.2, 1.8, 1, 0.02);
        a = Motion.Sample(a.Value, a.Velocity, 1, 0.03);
        var b = Motion.Sample(0.2, 1.8, 1, 0.05);
        Near(a.Value, b.Value, "Motion must use elapsed time, not callback count");
        Near(a.Velocity, b.Velocity, "Velocity must be independent of sample partition");
    }

    private static void WholeTreeCountsAndValidation()
    {
        var plan = new List<TaskNode> { Node("父任务中文", "pending",
            Node("完成一", "completed"), Node("完成二", "completed", Node("仍在等待", "blocked"))) };
        PlanModel.ValidateAndKey(plan);
        var counts = PlanModel.Count(plan);
        Check(counts.Completed == 2 && counts.Total == 4, "Count all nodes, including parents and collapsed descendants");
        Check(plan[0].Status == "pending", "Completed children must not complete the parent");
        Check(plan[0].Step == "父任务中文", "Unicode must survive normalization");
        Check(new HashSet<string>(Descendants(plan[0]).Select(n => n.Key).Concat(new[] { plan[0].Key })).Count == 4,
              "Each node needs a distinct hierarchical identity");
        Reject(() => PlanModel.ValidateAndKey(new List<TaskNode> { Node("bad", "success") }), "Invalid status accepted");
        Reject(() => PlanModel.ValidateAndKey(new List<TaskNode> { Node(" ", "pending") }), "Blank task accepted");
        Reject(() => PlanModel.ValidateAndKey(Enumerable.Range(0, 101).Select(i => Node(i.ToString(), "pending")).ToList()),
               "More than 100 nodes accepted");
        var deep = Node("level7", "pending");
        for (int depth = 6; depth >= 1; depth--) deep = Node("level" + depth, "pending", deep);
        Reject(() => PlanModel.ValidateAndKey(new List<TaskNode> { deep }), "More than six levels accepted");
    }

    private static void SingleBoundaryAndNaturalSpacing()
    {
        var parent = Node("父项", "in_progress", Enumerable.Range(0, 20).Select(i => Node("子项" + i, "pending")).ToArray());
        var sibling = Node("后续", "pending");
        var plan = new List<TaskNode> { parent, sibling };
        PlanModel.ValidateAndKey(plan);
        var heights = Heights(plan);
        var expansion = new Dictionary<string, MotionState>();
        foreach (double amount in new[] { 0.0, 0.03, 0.17, 0.33337, 0.6, 0.95, 1.0, 0.45, 0.0 })
        {
            expansion[parent.Key] = new MotionState(amount, 0);
            var layout = PlanLayout.Build(plan, heights, expansion);
            var children = parent.Children.Select(n => layout.Rows.Single(r => r.Key == n.Key)).ToList();
            OneBoundary(children);
            for (int i = 1; i < children.Count; i++)
                Near(children[i].Top - children[i - 1].Top, children[i - 1].NaturalHeight + 2, "Rows must retain natural spacing");
            foreach (var row in children) Near(row.NaturalHeight, heights[row.Key], "Animation must not squash text height");
            var p = layout.Rows.Single(r => r.Key == parent.Key);
            var s = layout.Rows.Single(r => r.Key == sibling.Key);
            Near(s.Top - p.Top, p.NaturalHeight + 2 + layout.ChildExtents[parent.Key] * amount, "Sibling follows the group's single reveal edge");
            if (amount == 0) Check(children.All(r => r.ClipHeight == 0), "Collapsed children must be fully clipped");
            if (amount == 1) Check(children.All(r => Math.Abs(r.ClipHeight - r.NaturalHeight) < 0.000001), "Expanded children must retain full height");
            Check(PlanModel.Count(plan).Completed == 0 && parent.Status == "in_progress", "Layout must not alter progress");
        }
    }

    private static void NestedBranchAndChatMemory()
    {
        var inner = Node("内层", "pending", Enumerable.Range(0, 8).Select(i => Node("内层子项" + i, "pending")).ToArray());
        var outer = Node("外层", "pending", Node("前项", "pending"), inner, Node("末项", "pending"));
        var plan = new List<TaskNode> { outer };
        PlanModel.ValidateAndKey(plan);
        var heights = Heights(plan);
        var expansions = new Dictionary<string, MotionState> { { inner.Key, new MotionState(1, 0) } };
        var memory = new ViewMemory();
        var first = memory.Get("chat-a");
        first.Collapsed = true; first.Scroll = 0.55; first.Expanded.Add(outer.Key); first.Expanded.Add(inner.Key);
        var second = memory.Get("chat-b");
        Check(!second.Collapsed && second.Scroll == 0 && second.Expanded.Count == 0, "New chat must not inherit another chat's view");
        second.Expanded.Add("other");
        foreach (double amount in new[] { 1.0, 0.6, 0.3, 0.0, 0.4, 1.0 })
        {
            expansions[outer.Key] = new MotionState(amount, 0);
            var layout = PlanLayout.Build(plan, heights, expansions);
            var descendants = Descendants(outer).Select(n => layout.Rows.Single(r => r.Key == n.Key)).ToList();
            OneBoundary(descendants);
            for (int i = 1; i < descendants.Count; i++)
                Near(descendants[i].Top - descendants[i - 1].Top, descendants[i - 1].NaturalHeight + 2,
                     "Expanded nested branch keeps natural descendant spacing");
            Near(expansions[inner.Key].Value, 1, "Ancestor collapse must not erase inner branch expansion");
        }
        var restored = memory.Get("chat-a");
        Check(restored.Collapsed && restored.Expanded.Contains(inner.Key) && restored.Expanded.Contains(outer.Key), "Chat view must survive a switch");
        Check(!restored.Expanded.Contains("other"), "Expanded keys must be isolated by chat");
        Near(restored.Scroll, 0.55, "Scroll memory must survive a switch");
    }

    private static void DragEdgesAndLogicalOffsets()
    {
        var bounds = new RectD(0, 0, 1000, 800);
        var clamped = DragMath.Clamp(new PointD(1654, 100), 280, 200, bounds);
        Near(clamped.X, 712, "Legacy oversized offset must clamp to the right edge");
        var firstReverse = DragMath.Move(clamped, new PointD(732, 110), new PointD(731, 110), bounds, 280, 200);
        Near(firstReverse.X, 711, "Moving left one pixel must immediately leave the edge");
        var edge = DragMath.Move(new PointD(704, 100), new PointD(724, 110), new PointD(1324, 110), bounds, 280, 200);
        Near(edge.X, 712, "Overdrag must stay within Codex bounds");
        var reversed = DragMath.Move(edge, new PointD(1324, 110), new PointD(1314, 110), bounds, 280, 200);
        Near(reversed.X, 702, "Discard unapplied edge movement before reversing");
        var offset = DragMath.Offset(reversed, bounds, 280, 1, 100, 16);
        Near(offset.X, -2, "Persist the applied logical horizontal offset");
        Near(offset.Y, 0, "Persist the applied logical vertical offset");
        var dpi = DragMath.Offset(new PointD(1505, 135), new RectD(0, 0, 2000, 1200), 420, 1.5, 70, 30);
        Near(dpi.X, -20, "DPI offset must be normalized exactly once");
        Near(dpi.Y, 20, "DPI vertical offset must be normalized exactly once");
        var leftMonitor = DragMath.Clamp(new PointD(-2000, -2000), 280, 200, new RectD(-1000, -500, 0, 300));
        Near(leftMonitor.X, -992, "Negative monitor coordinates must remain valid");
        Near(leftMonitor.Y, -492, "Negative monitor coordinates must remain valid");
    }

    private static void ActualSurfacePausesIconsAndReportsRealState()
    {
        var application = new System.Windows.Application();
        var window = new ProgressWindow("unused-view.json", System.IO.Path.GetTempPath(), 0, false, null);
        try
        {
            Check(new WindowInteropHelper(window).Handle == IntPtr.Zero, "This test must not create or show a native window");
            var surface = (ProgressSurface)typeof(ProgressWindow).GetField("surface", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(window);
            var tasks = new List<TaskNode> { Node("正在处理", "in_progress") };
            PlanModel.ValidateAndKey(tasks);
            surface.UpdateState("real-surface", "当前任务", "running", tasks, 300);
            Check(surface.HasAnimation, "Running work must animate");
            surface.UpdateState("real-surface", "当前任务", "idle", tasks, 300);
            Check(!surface.HasAnimation, "Idle work must not keep a render loop alive");
            double before = (double)typeof(ProgressSurface).GetField("phase", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(surface);
            surface.Advance(0.1);
            double after = (double)typeof(ProgressSurface).GetField("phase", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(surface);
            Near(after, before, "Paused icons must retain their phase during other layout work");
            surface.UpdateState("real-surface", "当前任务", "interrupted", tasks, 300);
            Check(!surface.HasAnimation, "Interrupted work must not keep a render loop alive");
            var complete = new List<TaskNode> { Node("完成任务", "completed") };
            PlanModel.ValidateAndKey(complete);
            surface.UpdateState("complete-chat", "当前任务", "running", complete, 300);
            var footer = (System.Windows.Media.FormattedText)typeof(ProgressSurface).GetField("footerText", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(surface);
            Check(footer.Text.Contains("全部完成"), "All-completed plans must not claim to still be running");
            var pending = new List<TaskNode> { Node("等待任务", "pending") };
            PlanModel.ValidateAndKey(pending);
            surface.UpdateState("pending-chat", "当前任务", "running", pending, 300);
            footer = (System.Windows.Media.FormattedText)typeof(ProgressSurface).GetField("footerText", BindingFlags.Instance | BindingFlags.NonPublic).GetValue(surface);
            Check(footer.Text.Contains("等待"), "Running turn without an active task must not claim execution");
            Check(!surface.HasAnimation, "A stable pending-only plan needs no render loop");
            Check(new WindowInteropHelper(window).Handle == IntPtr.Zero, "Surface checks must remain entirely off screen");
        }
        finally { window.Close(); }
    }

    [STAThread]
    public static int Main()
    {
        Console.OutputEncoding = new System.Text.UTF8Encoding(false);
        var tests = new Action[] { MotionContinuity, WholeTreeCountsAndValidation,
            SingleBoundaryAndNaturalSpacing, NestedBranchAndChatMemory, DragEdgesAndLogicalOffsets,
            ActualSurfacePausesIconsAndReportsRealState };
        foreach (var test in tests)
        {
            try { test(); Console.WriteLine("PASS " + test.Method.Name); }
            catch (Exception error) { Console.Error.WriteLine("FAIL " + test.Method.Name + ": " + error); return 1; }
        }
        return 0;
    }
}
