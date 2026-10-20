from __future__ import annotations

import bisect
import json
import os
import sys
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QSettings, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QFontMetrics, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QComboBox,
    QCheckBox,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QStatusBar,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QInputDialog,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .decoder import DbcInfo, LogDecoder
from .models import SignalId, SignalSeries


APP_ICON = Path(__file__).parent / "assets" / "log_review_icon.svg"


def format_relative_time(timestamp: float, include_sign: bool = False) -> str:
    """Format a relative timestamp without losing millisecond readability."""
    prefix = "+" if include_sign and timestamp >= 0 else ""
    if abs(timestamp) < 1.0:
        return f"{prefix}{timestamp * 1000:.3f} ms"
    return f"{prefix}{timestamp:.3f} s"


class RelativeTimeAxis(pg.AxisItem):
    """Use milliseconds on a sufficiently zoomed-in relative time axis."""

    def __init__(self) -> None:
        super().__init__(orientation="bottom")
        self._use_milliseconds = False

    def set_time_span(self, time_span: float) -> None:
        use_milliseconds = time_span < 1.0
        if self._use_milliseconds == use_milliseconds:
            return
        self._use_milliseconds = use_milliseconds
        self.setLabel("时间", units="ms" if use_milliseconds else "s")
        self.picture = None
        self.update()

    def tickStrings(self, values, scale, spacing):  # noqa: N802 - pyqtgraph API
        if self._use_milliseconds:
            return [f"{value * 1000:g}" for value in values]
        return [f"{value:g}" for value in values]


class SignalPlot(pg.PlotWidget):
    blue_cursor_moved = Signal(float)
    yellow_cursor_moved = Signal(float)
    cursor_placement_requested = Signal(float)

    def __init__(self, series: SignalSeries, end_time: float, color: str, show_title: bool) -> None:
        super().__init__(axisItems={"bottom": RelativeTimeAxis()})
        self.series = series
        self.color = color
        self._end_time = end_time
        self._title = f"{series.signal_id.message_name}.{series.signal_id.signal_name}  [CH{series.signal_id.channel}]"
        self._blue_cursor = self._create_cursor("#22bfff", "\u84dd")
        self._yellow_cursor = self._create_cursor("#ffd426", "\u9ec4")
        self._blue_cursor.sigPositionChanged.connect(
            lambda line: self.blue_cursor_moved.emit(float(line.value()))
        )
        self._yellow_cursor.sigPositionChanged.connect(
            lambda line: self.yellow_cursor_moved.emit(float(line.value()))
        )
        self.getPlotItem().scene().sigMouseClicked.connect(self._on_scene_clicked)
        self.getPlotItem().showGrid(x=True, y=True, alpha=0.25)
        self.getPlotItem().getViewBox().setBorder(pg.mkPen("#5a6570", width=1))
        self.getPlotItem().setLabel("left", series.unit or "Value")
        # Keep every stacked waveform's plotting frame aligned even when
        # their Y-axis tick labels have different widths.
        self.getPlotItem().getAxis("left").setWidth(82)
        self.getPlotItem().setLabel("bottom", "\u65f6\u95f4", units="s")
        self.getPlotItem().getViewBox().sigXRangeChanged.connect(self._on_x_range_changed)
        self.set_title_visible(show_title)

        x_values, y_values = self._build_step_data()
        curve = self.plot(x_values, y_values, pen=pg.mkPen(color, width=1.5))
        curve.setClipToView(True)
        curve.setDownsampling(auto=True, method="peak")
        self.setMinimumHeight(100)

    def set_title_visible(self, visible: bool) -> None:
        title = self._title if visible else None
        self.getPlotItem().setTitle(title, color="#e0e6ed", size="10pt")

    def set_grid_visible(self, visible: bool) -> None:
        self.getPlotItem().showGrid(x=visible, y=visible, alpha=0.25)

    def set_time_axis_visible(self, visible: bool) -> None:
        axis = self.getPlotItem().getAxis("bottom")
        axis.setStyle(showValues=visible)
        axis.showLabel(visible)
        axis.setVisible(visible)
        axis.setHeight(None if visible else 0)

    def _on_scene_clicked(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        point = self.getPlotItem().getViewBox().mapSceneToView(event.scenePos())
        self.cursor_placement_requested.emit(float(point.x()))

    def _on_x_range_changed(self, _view_box, x_range) -> None:
        axis = self.getPlotItem().getAxis("bottom")
        if isinstance(axis, RelativeTimeAxis):
            axis.set_time_span(max(0.0, float(x_range[1] - x_range[0])))

    def _create_cursor(self, color: str, label_prefix: str) -> pg.InfiniteLine:
        cursor = pg.InfiniteLine(
            angle=90,
            movable=True,
            pen=pg.mkPen(color, width=2),
            hoverPen=pg.mkPen("#f4f8fc", width=3),
            label=label_prefix,
            labelOpts={"color": "#071018", "fill": color, "position": 0.97},
        )
        cursor.setZValue(100)
        cursor.hide()
        self.addItem(cursor)
        return cursor

    def _build_step_data(self) -> tuple[list[float], list[float]]:
        times = self.series.timestamps
        values = self.series.values
        if not times:
            return [], []
        x_values = [times[0]]
        y_values = [values[0]]
        previous = values[0]
        for timestamp, value in zip(times[1:], values[1:]):
            x_values.extend((timestamp, timestamp))
            y_values.extend((previous, value))
            previous = value
        x_values.append(max(self._end_time, times[-1]))
        y_values.append(previous)
        return x_values, y_values

    def set_cursor_times(
        self,
        blue_time: float | None,
        yellow_time: float | None,
        show_labels: bool,
    ) -> None:
        self._set_cursor_time(self._blue_cursor, blue_time, show_labels, "\u84dd")
        self._set_cursor_time(self._yellow_cursor, yellow_time, show_labels, "\u9ec4")

    @staticmethod
    def _set_cursor_time(
        cursor: pg.InfiniteLine,
        timestamp: float | None,
        show_label: bool,
        label_prefix: str,
    ) -> None:
        cursor.setVisible(timestamp is not None)
        if timestamp is not None:
            cursor.blockSignals(True)
            cursor.setValue(timestamp)
            cursor.blockSignals(False)
            cursor.label.setFormat(f"{label_prefix} {format_relative_time(timestamp)}")
        cursor.label.setVisible(timestamp is not None and show_label)


class MainWindow(QMainWindow):
    COLORS = ("#00d5ff", "#ffcf33", "#ea4aaa", "#66e16c", "#fc8c3a", "#ac7dff")
    EDGE_DOCK_WIDTH = 30
    NAVIGATION_MIN_WIDTH = 280
    DETAILS_MIN_WIDTH = 240

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CANQT")
        self.setWindowIcon(QIcon(str(APP_ICON)))
        self.resize(1500, 900)
        self.settings = QSettings("EDY", "BLF_DBC_Log_Review")
        local_app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        self.user_data_dir = local_app_data / "BLF_DBC_Log_Review"
        self.workspace_file = self.user_data_dir / "workspace.json"
        self.current_project_path: Path | None = None
        self._pending_project_view: dict[str, object] | None = None
        self._restoring_workspace = False
        self._navigation_collapsed = False
        self._details_collapsed = False
        self._navigation_last_width = 390
        self._details_last_width = 300
        self._changing_docks = False

        self.decoder = LogDecoder()
        self.current_blf_path: str | None = None
        self.blf_library: dict[str, dict[str, object]] = {}
        self.channel_counts: dict[int, int] = {}
        self.channel_bindings: dict[int, str | None] = {}
        self.series: dict[SignalId, SignalSeries] = {}
        self.end_time = 0.0
        self.plots: dict[SignalId, SignalPlot] = {}
        self._channel_combos: dict[int, QComboBox] = {}
        self.show_signal_titles = True
        self.show_grid = True
        self.cursor_enabled = False
        self.cursor_placement_mode: str | None = None
        self.blue_cursor_time: float | None = None
        self.yellow_cursor_time: float | None = None
        self._tree_expansion_before_search: dict[tuple[str, str, str], bool] | None = None
        self.signal_groups: dict[str, list[dict[str, object]]] = self._load_signal_groups()
        self.active_signal_group_name: str | None = None
        self._cursor_update_timer = QTimer(self)
        self._cursor_update_timer.setSingleShot(True)
        self._cursor_update_timer.timeout.connect(self._apply_cursor_update)
        self._replay_header_text = "\u5f53\u524d\u672a\u9009\u62e9 BLF \u65e5\u5fd7"

        self._build_ui()
        self._build_actions()
        QTimer.singleShot(0, self._restore_workspace)

    def _build_ui(self) -> None:
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setHandleWidth(2)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setStyleSheet(
            "QSplitter::handle { background: #4f5a64; }"
            "QSplitter::handle:hover { background: #2d8cff; }"
        )
        self.setCentralWidget(self.main_splitter)

        self.navigation_tabs = QTabWidget()
        self.navigation_tabs.addTab(self._build_resources_page(), "\u8d44\u6e90\u4e0e\u7ed1\u5b9a")
        self.navigation_tabs.addTab(self._build_signals_page(), "\u4fe1\u53f7")
        self.navigation_tabs.addTab(self._build_signal_groups_page(), "观测组")
        self.navigation_dock, self.navigation_edge_button = self._build_edge_dock(
            self.navigation_tabs,
            "▶",
            "\u5c55\u5f00\u5de6\u4fa7\u8d44\u6e90\u4e0e\u4fe1\u53f7\u5de5\u4f5c\u533a",
        )
        self.navigation_dock.setMinimumWidth(self.EDGE_DOCK_WIDTH)
        self.main_splitter.addWidget(self.navigation_dock)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(6, 6, 6, 6)
        self.replay_header = QLabel(self._replay_header_text)
        self.replay_header.setStyleSheet(
            "font-weight: bold; padding: 6px 10px; color: #f4f8fc; "
            "background: #29425a; border: 1px solid #426884;"
        )
        self.replay_header.setToolTip(self._replay_header_text)
        center_layout.addWidget(self.replay_header)
        self.graphic_header = QLabel("\u5df2\u9009\u62e9 0 \u4e2a\u4fe1\u53f7")
        self.graphic_header.setStyleSheet("padding: 3px 6px; color: #c7d1db; background: #2a2f35;")
        center_layout.addWidget(self.graphic_header)
        center_layout.addWidget(self._build_cursor_toolbar())

        self.plot_splitter = QSplitter(Qt.Orientation.Vertical)
        self.plot_splitter.setHandleWidth(1)
        self.plot_splitter.setChildrenCollapsible(False)
        self.plot_splitter.setStyleSheet(
            "QSplitter { background: #050709; }"
            "QSplitter::handle:vertical { background: #71808b; }"
            "QSplitter::handle:vertical:hover { background: #2d8cff; }"
        )
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { border: 0px; background: #050709; }")
        self.scroll.setWidget(self.plot_splitter)
        self.scroll.hide()
        center_layout.addWidget(self.scroll)
        center.setMinimumWidth(520)
        self.main_splitter.addWidget(center)

        self.details_panel = self._build_details_panel()
        self.details_dock, self.details_edge_button = self._build_edge_dock(
            self.details_panel,
            "◀",
            "\u5c55\u5f00\u53f3\u4fa7\u901a\u9053\u7ed1\u5b9a\u4e0e\u6e38\u6807\u4fe1\u606f\u680f",
        )
        self.details_dock.setMinimumWidth(self.EDGE_DOCK_WIDTH)
        self.main_splitter.addWidget(self.details_dock)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([390, 1000, 300])
        self.main_splitter.splitterMoved.connect(self._on_main_splitter_moved)
        self.main_splitter.handle(1).installEventFilter(self)
        self.main_splitter.handle(2).installEventFilter(self)
        self.navigation_edge_button.clicked.connect(self.expand_navigation)
        self.details_edge_button.clicked.connect(self.expand_details)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("\u8bf7\u5148\u6dfb\u52a0 DBC\uff0c\u518d\u6253\u5f00 BLF\u3002")

    def _build_cursor_toolbar(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet("background: #292e34; border-radius: 3px;")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(8)
        self.cursor_enable_button = QPushButton("\u6e38\u6807\uff1a\u5173\u95ed")
        self.cursor_enable_button.setCheckable(True)
        self.cursor_enable_button.setStyleSheet(
            "QPushButton { background: #3a4650; color: #dce7ef; font-weight: bold; padding: 5px 12px; border: 0px; }"
            "QPushButton:checked { background: #3f5363; color: white; }"
        )
        self.cursor_enable_button.toggled.connect(self.set_cursor_enabled)
        self.place_blue_button = QPushButton("\u653e\u7f6e\u84dd\u6e38\u6807")
        self.place_blue_button.setCheckable(True)
        self.place_blue_button.setEnabled(False)
        self.place_blue_button.setStyleSheet(
            "QPushButton { background: #18506b; color: #bdefff; font-weight: bold; padding: 5px 12px; border: 0px; }"
            "QPushButton:checked { background: #22bfff; color: #071018; }"
        )
        self.place_blue_button.clicked.connect(lambda: self.set_cursor_placement_mode("blue"))
        self.place_yellow_button = QPushButton("\u653e\u7f6e\u9ec4\u6e38\u6807")
        self.place_yellow_button.setCheckable(True)
        self.place_yellow_button.setEnabled(False)
        self.place_yellow_button.setStyleSheet(
            "QPushButton { background: #72590d; color: #fff0a6; font-weight: bold; padding: 5px 12px; border: 0px; }"
            "QPushButton:checked { background: #ffd426; color: #2a2100; }"
        )
        self.place_yellow_button.clicked.connect(lambda: self.set_cursor_placement_mode("yellow"))
        self.clear_cursors_button = QPushButton("\u6e05\u9664\u6e38\u6807")
        self.clear_cursors_button.setEnabled(False)
        self.clear_cursors_button.setStyleSheet(
            "QPushButton { background: #3a4650; color: #dce7ef; font-weight: bold; padding: 5px 12px; border: 0px; }"
            "QPushButton:hover { background: #55636f; color: white; }"
        )
        self.clear_cursors_button.clicked.connect(self.clear_cursors)
        layout.addWidget(self.cursor_enable_button)
        layout.addWidget(self.place_blue_button)
        layout.addWidget(self.place_yellow_button)
        layout.addWidget(self.clear_cursors_button)
        layout.addStretch(1)
        return panel

    def _build_edge_dock(self, content: QWidget, button_text: str, tooltip: str) -> tuple[QWidget, QPushButton]:
        dock = QWidget()
        layout = QVBoxLayout(dock)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        edge_button = QPushButton(button_text)
        edge_button.setToolTip(tooltip)
        edge_button.setStyleSheet(
            "QPushButton { color: #dce9f4; background: #2a3742; border: 0px; font-size: 18px; }"
            "QPushButton:hover { background: #2d8cff; color: white; }"
        )
        edge_button.hide()
        layout.addWidget(content)
        layout.addWidget(edge_button, 1)
        return dock, edge_button

    def _on_main_splitter_moved(self, _position: int, _index: int) -> None:
        if self._changing_docks:
            return
        sizes = self.main_splitter.sizes()
        if not self._navigation_collapsed and sizes[0] <= self.EDGE_DOCK_WIDTH + 12:
            self.collapse_navigation()
        elif not self._navigation_collapsed:
            self._navigation_last_width = sizes[0]
        if not self._details_collapsed and sizes[2] <= self.EDGE_DOCK_WIDTH + 12:
            self.collapse_details()
        elif not self._details_collapsed:
            self._details_last_width = sizes[2]

    def collapse_navigation(self) -> None:
        if self._navigation_collapsed:
            return
        if self.navigation_dock.width() >= self.NAVIGATION_MIN_WIDTH:
            self._navigation_last_width = self.navigation_dock.width()
        self._navigation_collapsed = True
        self.navigation_tabs.hide()
        self.navigation_edge_button.show()
        self._set_dock_width(self.navigation_dock, 0, self.EDGE_DOCK_WIDTH)
        self._set_action_checked(self.toggle_navigation_action, False)

    def expand_navigation(self) -> None:
        if not self._navigation_collapsed:
            return
        self._navigation_collapsed = False
        self.navigation_dock.setMaximumWidth(16777215)
        self.navigation_dock.setMinimumWidth(self.EDGE_DOCK_WIDTH)
        self.navigation_edge_button.hide()
        self.navigation_tabs.show()
        self._resize_dock(0, self._navigation_last_width)
        self._set_action_checked(self.toggle_navigation_action, True)

    def collapse_details(self) -> None:
        if self._details_collapsed:
            return
        if self.details_dock.width() >= self.DETAILS_MIN_WIDTH:
            self._details_last_width = self.details_dock.width()
        self._details_collapsed = True
        self.details_panel.hide()
        self.details_edge_button.show()
        self._set_dock_width(self.details_dock, 2, self.EDGE_DOCK_WIDTH)
        self._set_action_checked(self.toggle_details_action, False)

    def expand_details(self) -> None:
        if not self._details_collapsed:
            return
        self._details_collapsed = False
        self.details_dock.setMaximumWidth(16777215)
        self.details_dock.setMinimumWidth(self.EDGE_DOCK_WIDTH)
        self.details_edge_button.hide()
        self.details_panel.show()
        self._resize_dock(2, self._details_last_width)
        self._set_action_checked(self.toggle_details_action, True)

    def _set_dock_width(self, dock: QWidget, index: int, width: int) -> None:
        self._changing_docks = True
        dock.setMinimumWidth(width)
        dock.setMaximumWidth(width)
        self._resize_dock(index, width)
        self._changing_docks = False

    def _resize_dock(self, index: int, width: int) -> None:
        sizes = self.main_splitter.sizes()
        released = max(0, sizes[index] - width)
        sizes[index] = width
        sizes[1] += released
        self.main_splitter.setSizes(sizes)

    @staticmethod
    def _set_action_checked(action: QAction, checked: bool) -> None:
        action.blockSignals(True)
        action.setChecked(checked)
        action.blockSignals(False)

    def _set_navigation_visible(self, visible: bool) -> None:
        if visible:
            self.expand_navigation()
        else:
            self.collapse_navigation()

    def _set_details_visible(self, visible: bool) -> None:
        if visible:
            self.expand_details()
        else:
            self.collapse_details()

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.MouseButtonDblClick:
            if watched is self.main_splitter.handle(1):
                self._set_navigation_visible(self._navigation_collapsed)
                return True
            if watched is self.main_splitter.handle(2):
                self._set_details_visible(self._details_collapsed)
                return True
        return super().eventFilter(watched, event)

    def _build_details_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)

        binding_group = QGroupBox("\u5f53\u524d\u901a\u9053\u7ed1\u5b9a")
        binding_layout = QVBoxLayout(binding_group)
        self.binding_details = QLabel("\u6682\u65e0\u7ed1\u5b9a")
        self.binding_details.setWordWrap(True)
        self.binding_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        binding_layout.addWidget(self.binding_details)
        layout.addWidget(binding_group)

        cursor_group = QGroupBox("\u53cc\u6e38\u6807\u4fe1\u53f7\u503c")
        cursor_layout = QVBoxLayout(cursor_group)
        self.cursor_summary = QLabel("\u6e38\u6807\u672a\u542f\u7528")
        self.cursor_summary.setWordWrap(True)
        self.cursor_summary.setStyleSheet("background: #30363c; color: #b8c6d0; padding: 10px; border-radius: 4px;")
        cursor_layout.addWidget(self.cursor_summary)
        self.cursor_table = QTableWidget(0, 4)
        self.cursor_table.setHorizontalHeaderLabels(["\u4fe1\u53f7", "\u84dd", "\u9ec4", "\u0394 \u9ec4-\u84dd"])
        self.cursor_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.cursor_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.cursor_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.cursor_table.verticalHeader().setVisible(False)
        self.cursor_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.cursor_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.cursor_table.setStyleSheet(
            "QTableWidget { background: #24282d; color: #dce5ed; border: 0px; gridline-color: #4d5963; }"
            "QHeaderView::section { background: #24282d; color: #b8c6d0; border: 0px; border-bottom: 1px solid #657581; padding: 5px; }"
        )
        cursor_layout.addWidget(self.cursor_table, 1)
        hint = QLabel("\u5dee\u503c\u5b9a\u4e49\uff1a\u9ec4\u6e38\u6807\u503c \u2212 \u84dd\u6e38\u6807\u503c")
        hint.setStyleSheet("color: #9faeba; padding-top: 4px;")
        cursor_layout.addWidget(hint)
        layout.addWidget(cursor_group, 1)
        return panel

    def _build_resources_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("resourcesPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        page.setStyleSheet(
            "QWidget#resourcesPage { background: #1b2026; }"
            "QFrame#resourceCard { background: #232a32; border: 1px solid #3c4651; border-radius: 5px; }"
            "QLabel#resourceTitle { color: #edf4fa; font-size: 14px; font-weight: bold; }"
            "QLabel#resourceHint { color: #91a1af; }"
            "QPushButton#resourceNav { min-height: 42px; background: #27313a; color: #dce7ef; "
            " border: 1px solid #46525e; border-radius: 4px; font-weight: bold; }"
            "QPushButton#resourceNav:hover { background: #31404c; border-color: #39aee8; }"
            "QPushButton#resourceNav:checked { background: #168fca; color: white; border-color: #3cc1ff; }"
            "QPushButton#primaryAction { min-height: 36px; background: #199fe3; color: white; "
            " border: 1px solid #43bdf5; border-radius: 4px; font-weight: bold; }"
            "QPushButton#primaryAction:hover { background: #25aceb; }"
            "QPushButton#primaryAction:disabled { background: #343c44; color: #77838d; border-color: #46515a; }"
        )

        nav = QFrame()
        nav.setObjectName("resourceCard")
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(7, 7, 7, 7)
        nav_layout.setSpacing(7)
        self.resource_nav_group = QButtonGroup(self)
        self.resource_nav_group.setExclusive(True)
        for index, text in enumerate(("DBC", "BLF", "绑定", "解析")):
            button = QPushButton(text)
            button.setObjectName("resourceNav")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, page_index=index: self._show_resource_page(page_index))
            self.resource_nav_group.addButton(button, index)
            nav_layout.addWidget(button)
            if index == 0:
                button.setChecked(True)
        layout.addWidget(nav)

        self.resource_stack = QStackedWidget()
        layout.addWidget(self.resource_stack, 1)

        dbc_page = QFrame()
        dbc_page.setObjectName("resourceCard")
        dbc_layout = QVBoxLayout(dbc_page)
        dbc_header = QHBoxLayout()
        dbc_title = QLabel("DBC 资源库")
        dbc_title.setObjectName("resourceTitle")
        dbc_header.addWidget(dbc_title)
        dbc_header.addStretch(1)
        self.dbc_summary_label = QLabel("已保存 0 个 DBC")
        self.dbc_summary_label.setObjectName("resourceHint")
        dbc_header.addWidget(self.dbc_summary_label)
        dbc_layout.addLayout(dbc_header)
        dbc_buttons = QHBoxLayout()
        add_dbc = QPushButton("+ 添加 DBC")
        add_dbc.clicked.connect(self.add_dbc)
        remove_dbc = QPushButton("移除所选")
        remove_dbc.clicked.connect(self.remove_selected_dbc)
        dbc_buttons.addWidget(add_dbc)
        dbc_buttons.addWidget(remove_dbc)
        dbc_buttons.addStretch(1)
        dbc_layout.addLayout(dbc_buttons)
        self.dbc_search = QLineEdit()
        self.dbc_search.setPlaceholderText("搜索 DBC 文件…")
        self.dbc_search.textChanged.connect(self._filter_dbc_table)
        dbc_layout.addWidget(self.dbc_search)
        self.dbc_list = QTableWidget(0, 4)
        self.dbc_list.setHorizontalHeaderLabels(["文件名", "报文数", "路径", "状态"])
        self.dbc_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.dbc_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.dbc_list.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.dbc_list.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.dbc_list.verticalHeader().setVisible(False)
        self.dbc_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.dbc_list.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.dbc_list.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        dbc_layout.addWidget(self.dbc_list)
        dbc_hint = QLabel("DBC 资源会自动保存，下次启动无需重新添加")
        dbc_hint.setObjectName("resourceHint")
        dbc_layout.addWidget(dbc_hint)
        self.resource_stack.addWidget(dbc_page)

        blf_page = QFrame()
        blf_page.setObjectName("resourceCard")
        blf_layout = QVBoxLayout(blf_page)
        blf_header = QHBoxLayout()
        log_title = QLabel("BLF 日志库")
        log_title.setObjectName("resourceTitle")
        blf_header.addWidget(log_title)
        blf_header.addStretch(1)
        self.blf_summary_label = QLabel("已保存 0 个日志")
        self.blf_summary_label.setObjectName("resourceHint")
        blf_header.addWidget(self.blf_summary_label)
        blf_layout.addLayout(blf_header)
        blf_buttons = QHBoxLayout()
        add_blf = QPushButton("+ 添加 BLF")
        add_blf.clicked.connect(self.open_blf)
        remove_blf = QPushButton("移除所选")
        remove_blf.clicked.connect(self.remove_selected_blf)
        blf_buttons.addWidget(add_blf)
        blf_buttons.addWidget(remove_blf)
        blf_buttons.addStretch(1)
        blf_layout.addLayout(blf_buttons)
        self.blf_search = QLineEdit()
        self.blf_search.setPlaceholderText("搜索 BLF 文件…")
        self.blf_search.textChanged.connect(self._filter_blf_table)
        blf_layout.addWidget(self.blf_search)
        self.blf_table = QTableWidget(0, 5)
        self.blf_table.setHorizontalHeaderLabels(["文件名", "大小", "时长", "通道", "状态"])
        self.blf_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 4):
            self.blf_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.blf_table.verticalHeader().setVisible(False)
        self.blf_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.blf_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.blf_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.blf_table.itemSelectionChanged.connect(self._activate_selected_blf)
        blf_layout.addWidget(self.blf_table, 1)
        current_blf_card = QFrame()
        current_blf_card.setObjectName("resourceCard")
        current_blf_layout = QVBoxLayout(current_blf_card)
        self.blf_label = QLabel("\u672a\u9009\u62e9 BLF")
        self.blf_label.setWordWrap(True)
        current_blf_layout.addWidget(self.blf_label)
        self.blf_status_label = QLabel("\u672a\u9009\u62e9")
        self.blf_status_label.setObjectName("resourceHint")
        current_blf_layout.addWidget(self.blf_status_label)
        blf_layout.addWidget(current_blf_card)
        self.resource_stack.addWidget(blf_page)

        binding_page = QFrame()
        binding_page.setObjectName("resourceCard")
        binding_layout = QVBoxLayout(binding_page)
        binding_title = QLabel("\u901a\u9053\u7ed1\u5b9a")
        binding_title.setObjectName("resourceTitle")
        binding_layout.addWidget(binding_title)
        self.binding_blf_label = QLabel("当前 BLF：未选择")
        self.binding_blf_label.setWordWrap(True)
        binding_layout.addWidget(self.binding_blf_label)
        binding_hint = QLabel("\u53ea\u89e3\u6790\u5df2\u7ed1\u5b9a\u901a\u9053\uff1b\u672a\u7ed1\u5b9a\u901a\u9053\u5c06\u81ea\u52a8\u8df3\u8fc7\u3002")
        binding_hint.setObjectName("resourceHint")
        binding_hint.setWordWrap(True)
        binding_layout.addWidget(binding_hint)
        self.channel_table = QTableWidget(0, 4)
        self.channel_table.setHorizontalHeaderLabels(["\u901a\u9053", "\u5e27\u6570", "\u4f7f\u7528 DBC", "\u72b6\u6001"])
        self.channel_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.channel_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.channel_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.channel_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.channel_table.verticalHeader().setVisible(False)
        self.channel_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.channel_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        binding_layout.addWidget(self.channel_table, 1)
        self.binding_ready_label = QLabel("\u8bf7\u81f3\u5c11\u7ed1\u5b9a\u4e00\u4e2a\u901a\u9053")
        self.binding_ready_label.setObjectName("resourceHint")
        binding_layout.addWidget(self.binding_ready_label)
        clear_bindings = QPushButton("清除全部绑定")
        clear_bindings.clicked.connect(self.clear_all_bindings)
        binding_layout.addWidget(clear_bindings)
        self.resource_stack.addWidget(binding_page)

        parse_page = QFrame()
        parse_page.setObjectName("resourceCard")
        parse_layout = QVBoxLayout(parse_page)
        parse_title = QLabel("解析确认")
        parse_title.setObjectName("resourceTitle")
        parse_layout.addWidget(parse_title)
        parse_subtitle = QLabel("确认本次日志和通道配置")
        parse_subtitle.setObjectName("resourceHint")
        parse_layout.addWidget(parse_subtitle)
        self.parse_blf_label = QLabel("当前 BLF\n未选择")
        self.parse_blf_label.setWordWrap(True)
        parse_layout.addWidget(self.parse_blf_label)
        self.parse_bound_label = QLabel("参与解析\n暂无已绑定通道")
        self.parse_bound_label.setWordWrap(True)
        parse_layout.addWidget(self.parse_bound_label)
        self.parse_skipped_label = QLabel("跳过通道\n暂无")
        self.parse_skipped_label.setWordWrap(True)
        parse_layout.addWidget(self.parse_skipped_label)
        self.parse_summary_label = QLabel("扫描通道：0    参与通道：0    预计报文：0")
        self.parse_summary_label.setObjectName("resourceHint")
        self.parse_summary_label.setWordWrap(True)
        parse_layout.addWidget(self.parse_summary_label)
        parse_layout.addStretch(1)
        self.apply_button = QPushButton("▶ 解析当前日志")
        self.apply_button.setObjectName("primaryAction")
        self.apply_button.clicked.connect(self.apply_bindings_and_decode)
        self.apply_button.setEnabled(False)
        parse_layout.addWidget(self.apply_button)
        parse_hint = QLabel("解析完成后自动切换到信号页")
        parse_hint.setObjectName("resourceHint")
        parse_layout.addWidget(parse_hint)
        self.last_parse_label = QLabel("上次解析结果：暂无")
        self.last_parse_label.setObjectName("resourceHint")
        parse_layout.addWidget(self.last_parse_label)
        self.resource_stack.addWidget(parse_page)
        return page

    def _show_resource_page(self, index: int) -> None:
        self.resource_stack.setCurrentIndex(index)

    def _build_signals_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)

        view_buttons = QHBoxLayout()
        view_buttons.setSpacing(0)
        self.all_signals_button = QPushButton("全部信号")
        self.selected_signals_button = QPushButton("已选择 0")
        self.signal_view_group = QButtonGroup(self)
        self.signal_view_group.setExclusive(True)
        for index, button in enumerate((self.all_signals_button, self.selected_signals_button)):
            button.setCheckable(True)
            button.setObjectName("signalViewButton")
            button.clicked.connect(lambda _checked=False, page_index=index: self._show_signal_view(page_index))
            self.signal_view_group.addButton(button, index)
            view_buttons.addWidget(button)
        self.all_signals_button.setChecked(True)
        layout.addLayout(view_buttons)

        self.signal_view_stack = QStackedWidget()
        all_signals_page = QWidget()
        all_layout = QVBoxLayout(all_signals_page)
        all_layout.setContentsMargins(0, 8, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("\u641c\u7d22\u62a5\u6587\u6216\u4fe1\u53f7\u2026")
        self.search.textChanged.connect(self._filter_tree)
        all_layout.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["\u62a5\u6587 / \u4fe1\u53f7", "\u62a5\u6587 ID", "\u5355\u4f4d", "\u901a\u9053"])
        self.tree.itemChanged.connect(self._on_tree_changed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_signal_tree_context_menu)
        all_layout.addWidget(self.tree, 1)
        all_footer = QHBoxLayout()
        self.selected_status_label = QLabel("已选择 0 个信号")
        show_selected = QPushButton("查看已选择")
        show_selected.clicked.connect(lambda: self._show_signal_view(1))
        all_footer.addWidget(self.selected_status_label)
        all_footer.addStretch(1)
        all_footer.addWidget(show_selected)
        all_layout.addLayout(all_footer)
        self.signal_view_stack.addWidget(all_signals_page)

        selected_page = QWidget()
        selected_layout = QVBoxLayout(selected_page)
        selected_layout.setContentsMargins(0, 8, 0, 0)
        selected_actions = QHBoxLayout()
        show_all = QPushButton("全部显示")
        show_all.clicked.connect(lambda: self._set_all_selected_plots_visible(True))
        hide_all = QPushButton("全部隐藏")
        hide_all.clicked.connect(lambda: self._set_all_selected_plots_visible(False))
        clear_selected = QPushButton("清空选择")
        clear_selected.clicked.connect(self.clear_plots)
        selected_actions.addWidget(show_all)
        selected_actions.addWidget(hide_all)
        selected_actions.addWidget(clear_selected)
        selected_layout.addLayout(selected_actions)
        self.selected_signal_list = QListWidget()
        self.selected_signal_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.selected_signal_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.selected_signal_list.itemDoubleClicked.connect(
            lambda item: self._locate_signal_in_tree(item.data(Qt.ItemDataRole.UserRole))
        )
        self.selected_signal_list.model().rowsMoved.connect(
            lambda *_args: QTimer.singleShot(0, self._apply_selected_signal_order)
        )
        selected_layout.addWidget(self.selected_signal_list, 1)
        selected_hint = QLabel("拖动可调整波形顺序；双击信号可返回全部信号并定位")
        selected_hint.setStyleSheet("color: #91a1af; padding: 4px 0;")
        selected_layout.addWidget(selected_hint)
        self.signal_view_stack.addWidget(selected_page)
        layout.addWidget(self.signal_view_stack, 1)
        page.setStyleSheet(
            "QPushButton#signalViewButton { min-height: 34px; background: #27313a; color: #dce7ef; "
            "border: 1px solid #46525e; font-weight: bold; }"
            "QPushButton#signalViewButton:checked { background: #199fe3; color: white; border-color: #43bdf5; }"
        )
        return page

    def _show_signal_view(self, index: int) -> None:
        self.signal_view_stack.setCurrentIndex(index)
        button = self.signal_view_group.button(index)
        if button is not None:
            button.setChecked(True)
        if index == 1:
            self._refresh_selected_signals_ui()

    def _refresh_selected_signals_ui(self) -> None:
        if not hasattr(self, "selected_signal_list"):
            return
        count = len(self.plots)
        self.selected_signals_button.setText(f"已选择 {count}")
        self.selected_status_label.setText(f"已选择 {count} 个信号")
        self.selected_signal_list.clear()
        for signal_id, plot in self.plots.items():
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, signal_id)
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(5, 3, 3, 3)
            row_layout.setSpacing(6)
            drag_handle = QLabel("⋮⋮")
            drag_handle.setStyleSheet("color: #91a1af;")
            color_marker = QLabel("■")
            color_marker.setStyleSheet(f"color: {plot.color}; font-size: 16px;")
            name = QLabel(f"{signal_id.message_name}.{signal_id.signal_name}")
            name.setToolTip(name.text())
            metadata = QLabel(f"{plot.series.unit or '—'} / CH{signal_id.channel}")
            metadata.setStyleSheet("color: #aebbc6;")
            visibility = QPushButton("隐藏" if not plot.isHidden() else "显示")
            visibility.setFixedWidth(46)
            visibility.clicked.connect(
                lambda _checked=False, selected_id=signal_id: self._toggle_selected_plot_visibility(selected_id)
            )
            locate = QPushButton("定位")
            locate.setFixedWidth(42)
            locate.clicked.connect(
                lambda _checked=False, selected_id=signal_id: self._locate_signal_in_tree(selected_id)
            )
            remove = QPushButton("×")
            remove.setFixedWidth(28)
            remove.clicked.connect(
                lambda _checked=False, selected_id=signal_id: self._remove_selected_signal(selected_id)
            )
            row_layout.addWidget(drag_handle)
            row_layout.addWidget(color_marker)
            row_layout.addWidget(name, 1)
            row_layout.addWidget(metadata)
            row_layout.addWidget(visibility)
            row_layout.addWidget(locate)
            row_layout.addWidget(remove)
            item.setSizeHint(row.sizeHint())
            self.selected_signal_list.addItem(item)
            self.selected_signal_list.setItemWidget(item, row)

    def _toggle_selected_plot_visibility(self, signal_id: SignalId) -> None:
        plot = self.plots.get(signal_id)
        if plot is not None:
            self._set_selected_plot_visible(signal_id, plot.isHidden())

    def _set_selected_plot_visible(self, signal_id: SignalId, visible: bool) -> None:
        plot = self.plots.get(signal_id)
        if plot is None:
            return
        plot.setVisible(visible)
        self._update_time_axis_visibility()
        self._update_plot_area_minimum_height()
        self._distribute_plot_space()
        self._refresh_selected_signals_ui()

    def _set_all_selected_plots_visible(self, visible: bool) -> None:
        for plot in self.plots.values():
            plot.setVisible(visible)
        self._update_time_axis_visibility()
        self._update_plot_area_minimum_height()
        self._distribute_plot_space()
        self._refresh_selected_signals_ui()

    def _remove_selected_signal(self, signal_id: SignalId) -> None:
        self.tree.blockSignals(True)
        try:
            for parent_index in range(self.tree.topLevelItemCount()):
                parent = self.tree.topLevelItem(parent_index)
                for child_index in range(parent.childCount()):
                    child = parent.child(child_index)
                    if child.data(0, Qt.ItemDataRole.UserRole) == signal_id:
                        child.setCheckState(0, Qt.CheckState.Unchecked)
                        break
        finally:
            self.tree.blockSignals(False)
        self._remove_plot(signal_id)

    def _locate_signal_in_tree(self, signal_id: object) -> None:
        if not isinstance(signal_id, SignalId):
            return
        self._show_signal_view(0)
        self.search.setText(signal_id.signal_name)
        for parent_index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(parent_index)
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                if child.data(0, Qt.ItemDataRole.UserRole) == signal_id:
                    parent.setExpanded(True)
                    self.tree.setCurrentItem(child)
                    self.tree.scrollToItem(child)
                    return

    def _apply_selected_signal_order(self) -> None:
        ordered_ids = [
            self.selected_signal_list.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(self.selected_signal_list.count())
        ]
        if set(ordered_ids) != set(self.plots):
            return
        self.plots = {signal_id: self.plots[signal_id] for signal_id in ordered_ids}
        for index, plot in enumerate(self.plots.values()):
            self.plot_splitter.insertWidget(index, plot)
        self._update_time_axis_visibility()
        self._sync_cursor_lines()
        self._refresh_cursor_details()

    def _build_signal_groups_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)

        self.active_group_label = QLabel("当前未加载观测组")
        self.active_group_label.setStyleSheet("padding: 6px; color: #c7d1db; background: #2a2f35;")
        layout.addWidget(self.active_group_label)

        layout.addWidget(QLabel("双击观测组即可恢复波形"))
        self.signal_group_list = QListWidget()
        self.signal_group_list.currentItemChanged.connect(self._refresh_signal_group_members)
        self.signal_group_list.itemDoubleClicked.connect(lambda _item: self.apply_signal_group())
        self.signal_group_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.signal_group_list.customContextMenuRequested.connect(self._show_signal_group_menu)
        layout.addWidget(self.signal_group_list, 2)

        primary_buttons = QHBoxLayout()
        create_group = QPushButton("从当前波形新建")
        create_group.clicked.connect(self.save_current_signal_group)
        update_group = QPushButton("更新当前组")
        update_group.clicked.connect(self.update_current_signal_group)
        primary_buttons.addWidget(create_group)
        primary_buttons.addWidget(update_group)
        layout.addLayout(primary_buttons)

        apply_mode = QGroupBox("应用方式")
        apply_mode_layout = QHBoxLayout(apply_mode)
        self.replace_group_radio = QRadioButton("替换当前波形")
        self.append_group_radio = QRadioButton("添加到当前波形")
        self.replace_group_radio.setChecked(True)
        self.signal_group_mode = QButtonGroup(self)
        self.signal_group_mode.addButton(self.replace_group_radio)
        self.signal_group_mode.addButton(self.append_group_radio)
        apply_mode_layout.addWidget(self.replace_group_radio)
        apply_mode_layout.addWidget(self.append_group_radio)
        layout.addWidget(apply_mode)

        layout.addWidget(QLabel("组内信号（红色表示当前 DBC/日志中缺失）"))
        self.signal_group_members = QListWidget()
        layout.addWidget(self.signal_group_members, 3)
        member_buttons = QHBoxLayout()
        remove_member = QPushButton("移除")
        remove_member.clicked.connect(self.remove_signal_group_member)
        move_up = QPushButton("上移")
        move_up.clicked.connect(lambda: self.move_signal_group_member(-1))
        move_down = QPushButton("下移")
        move_down.clicked.connect(lambda: self.move_signal_group_member(1))
        member_buttons.addWidget(remove_member)
        member_buttons.addWidget(move_up)
        member_buttons.addWidget(move_down)
        layout.addLayout(member_buttons)

        self._refresh_signal_groups_ui()
        return page

    def _load_signal_groups(self) -> dict[str, list[dict[str, object]]]:
        raw = self.settings.value("signal_groups_json", "{}")
        try:
            stored = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError:
            return {}
        return self._normalise_signal_groups(stored)

    @staticmethod
    def _normalise_signal_groups(data: object) -> dict[str, list[dict[str, object]]]:
        if not isinstance(data, dict):
            return {}
        groups: dict[str, list[dict[str, object]]] = {}
        for name, items in data.items():
            if not isinstance(name, str) or not isinstance(items, list):
                continue
            valid_items = [item for item in items if isinstance(item, dict)]
            if valid_items:
                groups[name] = valid_items
        return groups

    def _save_signal_groups(self) -> None:
        self.settings.setValue("signal_groups_json", json.dumps(self.signal_groups, ensure_ascii=False))
        self.settings.sync()

    def _selected_signal_group_name(self) -> str | None:
        if not hasattr(self, "signal_group_list"):
            return None
        item = self.signal_group_list.currentItem()
        name = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return name if isinstance(name, str) else None

    def _refresh_signal_groups_ui(self, selected_name: str | None = None) -> None:
        if not hasattr(self, "signal_group_list"):
            return
        selected_name = selected_name or self._selected_signal_group_name() or self.active_signal_group_name
        self.signal_group_list.blockSignals(True)
        self.signal_group_list.clear()
        selected_item = None
        for name, items in sorted(self.signal_groups.items()):
            prefix = "● " if name == self.active_signal_group_name else ""
            item = QListWidgetItem(f"{prefix}{name}\n{len(items)} 个信号")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setToolTip("双击加载；右键可重命名、复制或删除。")
            self.signal_group_list.addItem(item)
            if name == selected_name:
                selected_item = item
        self.signal_group_list.blockSignals(False)
        if selected_item is not None:
            self.signal_group_list.setCurrentItem(selected_item)
        elif self.signal_group_list.count():
            self.signal_group_list.setCurrentRow(0)
        else:
            self.signal_group_members.clear()

    def _refresh_signal_group_members(self, current: QListWidgetItem | None = None, _previous=None) -> None:
        self.signal_group_members.clear()
        name = current.data(Qt.ItemDataRole.UserRole) if current is not None else self._selected_signal_group_name()
        if not isinstance(name, str):
            return
        for data in self.signal_groups.get(name, []):
            signal_id = self._signal_id_from_data(data)
            if signal_id is None:
                continue
            channel = "全部通道" if signal_id.channel is None else f"CH{signal_id.channel}"
            missing = bool(self.series) and signal_id not in self.series
            prefix = "⚠ 缺失  " if missing else ""
            item = QListWidgetItem(f"{prefix}{signal_id.message_name}.{signal_id.signal_name}  [{channel}]")
            item.setData(Qt.ItemDataRole.UserRole, data)
            if missing:
                item.setForeground(QColor("#ff6b6b"))
            self.signal_group_members.addItem(item)

    def save_current_signal_group(self) -> None:
        if not self.plots:
            self._show_error("无法保存信号组", "请先在波形区勾选至少一个信号。")
            return
        name, accepted = QInputDialog.getText(self, "保存信号组", "信号组名称：")
        name = name.strip()
        if not accepted or not name:
            return
        self.signal_groups[name] = [self._signal_id_to_data(signal_id) for signal_id in self.plots]
        self.active_signal_group_name = name
        self._save_signal_groups()
        self._refresh_signal_groups_ui(name)
        self.active_group_label.setText(f"当前组：{name}")
        self.statusBar().showMessage(f"已保存信号组“{name}”：{len(self.plots)} 个信号。")

    def update_current_signal_group(self) -> None:
        name = self._selected_signal_group_name()
        if name is None:
            self._show_error("无法更新观测组", "请先选择一个观测组。")
            return
        if not self.plots:
            self._show_error("无法更新观测组", "当前波形区没有信号。")
            return
        self.signal_groups[name] = [self._signal_id_to_data(signal_id) for signal_id in self.plots]
        self.active_signal_group_name = name
        self._save_signal_groups()
        self._refresh_signal_groups_ui(name)
        self.active_group_label.setText(f"当前组：{name}")
        self.statusBar().showMessage(f"已用当前波形更新观测组“{name}”。")

    def apply_signal_group(self) -> None:
        name = self._selected_signal_group_name()
        if name is None:
            return
        if not self.series:
            self._show_error("无法应用信号组", "请先应用绑定并解析 BLF。")
            return
        signal_ids = [self._signal_id_from_data(item) for item in self.signal_groups.get(name, [])]
        available = [signal_id for signal_id in signal_ids if signal_id in self.series]
        if self.replace_group_radio.isChecked():
            self.clear_plots()
        for signal_id in available:
            self._set_signal_tree_checked(signal_id)
            self._add_plot(signal_id)
        self.active_signal_group_name = name
        self.active_group_label.setText(f"当前组：{name}")
        self._refresh_signal_groups_ui(name)
        self.statusBar().showMessage(f"已应用信号组“{name}”：恢复 {len(available)} / {len(signal_ids)} 个信号。")

    def delete_signal_group(self) -> None:
        name = self._selected_signal_group_name()
        if name is None:
            return
        answer = QMessageBox.question(self, "删除观测组", f"确定删除观测组“{name}”吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.signal_groups.pop(name, None)
        if self.active_signal_group_name == name:
            self.active_signal_group_name = None
            self.active_group_label.setText("当前未加载观测组")
        self._save_signal_groups()
        self._refresh_signal_groups_ui()
        self.statusBar().showMessage(f"已删除信号组“{name}”。")

    def rename_signal_group(self) -> None:
        old_name = self._selected_signal_group_name()
        if old_name is None:
            return
        new_name, accepted = QInputDialog.getText(self, "重命名观测组", "新名称：", text=old_name)
        new_name = new_name.strip()
        if not accepted or not new_name or new_name == old_name:
            return
        if new_name in self.signal_groups:
            self._show_error("无法重命名", "已经存在同名观测组。")
            return
        self.signal_groups[new_name] = self.signal_groups.pop(old_name)
        if self.active_signal_group_name == old_name:
            self.active_signal_group_name = new_name
            self.active_group_label.setText(f"当前组：{new_name}")
        self._save_signal_groups()
        self._refresh_signal_groups_ui(new_name)

    def duplicate_signal_group(self) -> None:
        source_name = self._selected_signal_group_name()
        if source_name is None:
            return
        new_name, accepted = QInputDialog.getText(self, "复制观测组", "新组名称：", text=f"{source_name} 副本")
        new_name = new_name.strip()
        if not accepted or not new_name:
            return
        if new_name in self.signal_groups:
            self._show_error("无法复制", "已经存在同名观测组。")
            return
        self.signal_groups[new_name] = [dict(item) for item in self.signal_groups[source_name]]
        self._save_signal_groups()
        self._refresh_signal_groups_ui(new_name)

    def _show_signal_group_menu(self, position) -> None:
        if self.signal_group_list.itemAt(position) is None:
            return
        menu = QMenu(self)
        menu.addAction("加载", self.apply_signal_group)
        menu.addAction("重命名", self.rename_signal_group)
        menu.addAction("复制", self.duplicate_signal_group)
        menu.addSeparator()
        menu.addAction("删除", self.delete_signal_group)
        menu.exec(self.signal_group_list.mapToGlobal(position))

    def remove_signal_group_member(self) -> None:
        name = self._selected_signal_group_name()
        row = self.signal_group_members.currentRow()
        if name is None or row < 0:
            return
        self.signal_groups[name].pop(row)
        if not self.signal_groups[name]:
            self.signal_groups.pop(name)
        self._save_signal_groups()
        self._refresh_signal_groups_ui(name)

    def move_signal_group_member(self, direction: int) -> None:
        name = self._selected_signal_group_name()
        row = self.signal_group_members.currentRow()
        if name is None or row < 0:
            return
        target = row + direction
        items = self.signal_groups[name]
        if target < 0 or target >= len(items):
            return
        items[row], items[target] = items[target], items[row]
        self._save_signal_groups()
        self._refresh_signal_groups_ui(name)
        self.signal_group_members.setCurrentRow(target)

    def _show_signal_tree_context_menu(self, position) -> None:
        item = self.tree.itemAt(position)
        signal_id = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(signal_id, SignalId):
            return
        menu = QMenu(self)
        add_menu = menu.addMenu("加入观测组")
        if not self.signal_groups:
            empty_action = add_menu.addAction("请先在“观测组”页新建组")
            empty_action.setEnabled(False)
        else:
            for name in sorted(self.signal_groups):
                action = add_menu.addAction(name)
                action.triggered.connect(
                    lambda _checked=False, group_name=name, selected_signal=signal_id:
                    self._add_signal_to_group(group_name, selected_signal)
                )
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _add_signal_to_group(self, name: str, signal_id: SignalId) -> None:
        data = self._signal_id_to_data(signal_id)
        if data not in self.signal_groups[name]:
            self.signal_groups[name].append(data)
            self._save_signal_groups()
        self._refresh_signal_groups_ui(name)
        self.statusBar().showMessage(f"已将 {signal_id.signal_name} 加入观测组“{name}”。")

    @staticmethod
    def _signal_id_to_data(signal_id: SignalId) -> dict[str, object]:
        return {
            "message_name": signal_id.message_name,
            "signal_name": signal_id.signal_name,
            "channel": signal_id.channel,
            "frame_id": signal_id.frame_id,
            "is_extended_frame": signal_id.is_extended_frame,
        }

    @staticmethod
    def _signal_id_from_data(data: object) -> SignalId | None:
        if not isinstance(data, dict):
            return None
        message_name = data.get("message_name")
        signal_name = data.get("signal_name")
        channel = data.get("channel")
        frame_id = data.get("frame_id")
        if not isinstance(message_name, str) or not isinstance(signal_name, str):
            return None
        if channel is not None and not isinstance(channel, int):
            return None
        if frame_id is not None and not isinstance(frame_id, int):
            return None
        return SignalId(message_name, signal_name, channel, frame_id, bool(data.get("is_extended_frame", False)))

    def _project_data(self) -> dict[str, object]:
        blf_data: dict[str, object] | None = None
        if self.current_blf_path is not None:
            blf_data = {
                "path": self.current_blf_path,
                "channels": {str(channel): count for channel, count in self.channel_counts.items()},
                "bindings": {str(channel): path for channel, path in self.channel_bindings.items()},
                "signature": self._file_signature(self.current_blf_path),
            }
        time_range = None
        if self.plots:
            time_range = list(next(iter(self.plots.values())).getViewBox().viewRange()[0])
        return {
            "version": 1,
            "dbc_paths": [info.path for info in self.decoder.dbc_files],
            "blf": blf_data,
            "signal_groups": self.signal_groups,
            "view": {
                "selected_signals": [self._signal_id_to_data(signal_id) for signal_id in self.plots],
                "show_signal_titles": self.show_signal_titles,
                "show_grid": self.show_grid,
                "time_range": time_range,
                "cursor_enabled": self.cursor_enabled,
                "blue_cursor_time": self.blue_cursor_time,
                "yellow_cursor_time": self.yellow_cursor_time,
            },
        }

    @staticmethod
    def _write_json(path: Path, data: dict[str, object]) -> None:
        temporary_file = path.with_suffix(path.suffix + ".tmp")
        temporary_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_file.replace(path)

    def new_project(self) -> None:
        self.decoder = LogDecoder()
        self.current_project_path = None
        self.current_blf_path = None
        self.channel_counts = {}
        self.channel_bindings = {}
        self._pending_project_view = None
        self._clear_decoded_data()
        self._refresh_current_blf_labels()
        self._refresh_blf_library()
        self._refresh_dbc_list()
        self._refresh_channel_table()
        self._refresh_replay_header()
        self._save_resource_workspace()
        self.statusBar().showMessage("已新建空工程。")

    def save_project(self) -> None:
        if self.current_project_path is None:
            self.save_project_as()
            return
        try:
            self._write_json(self.current_project_path, self._project_data())
        except OSError as error:
            self._show_error("工程保存失败", str(error))
            return
        self.statusBar().showMessage(f"工程已保存：{self.current_project_path.name}")

    def save_project_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "保存 CANQT 工程", "", "CANQT Project (*.canqt)")
        if not path:
            return
        self.current_project_path = Path(path).with_suffix(".canqt")
        self.save_project()

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "打开 CANQT 工程", "", "CANQT Project (*.canqt)")
        if not path:
            return
        project_path = Path(path)
        try:
            data = json.loads(project_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            self._show_error("工程打开失败", str(error))
            return
        if not isinstance(data, dict):
            self._show_error("工程打开失败", "工程文件内容无效。")
            return
        self._load_project_data(data)
        self.current_project_path = project_path
        self.statusBar().showMessage(f"已打开工程：{project_path.name}。请手动点击“解析当前日志”。")

    def _load_project_data(self, data: dict[str, object]) -> None:
        self._restoring_workspace = True
        try:
            self.decoder = LogDecoder()
            self.current_blf_path = None
            self.channel_counts = {}
            self.channel_bindings = {}
            self._clear_decoded_data()
            dbc_paths = [path for path in data.get("dbc_paths", []) if isinstance(path, str) and Path(path).is_file()]
            if dbc_paths:
                self.decoder.add_dbc_files(dbc_paths)
            self._refresh_dbc_list()
            groups = data.get("signal_groups")
            if groups is not None:
                self.signal_groups = self._normalise_signal_groups(groups)
                self._save_signal_groups()
                self._refresh_signal_groups_ui()
            blf_data = data.get("blf")
            if isinstance(blf_data, dict):
                blf_path = blf_data.get("path")
                saved_channels = blf_data.get("channels", {})
                saved_bindings = blf_data.get("bindings", {})
                if isinstance(blf_path, str) and Path(blf_path).is_file() and isinstance(saved_channels, dict) and isinstance(saved_bindings, dict):
                    self.current_blf_path = blf_path
                    self.channel_counts = {int(channel): int(count) for channel, count in saved_channels.items()}
                    available_paths = {info.path for info in self.decoder.dbc_files}
                    self.channel_bindings = {
                        channel: saved_bindings.get(str(channel)) if saved_bindings.get(str(channel)) in available_paths else None
                        for channel in self.channel_counts
                    }
                    self.blf_label.setText(Path(blf_path).name)
                    self.blf_label.setToolTip(blf_path)
                    self.blf_status_label.setText(f"已恢复 {len(self.channel_counts)} 个通道")
                    self.blf_library[blf_path] = {
                        "path": blf_path,
                        "channels": dict(self.channel_counts),
                        "bindings": dict(self.channel_bindings),
                        "duration": 0.0,
                    }
                else:
                    self.blf_label.setText("工程中的 BLF 文件不存在，请重新选择。")
                    self.blf_status_label.setText("文件不存在")
            else:
                self.blf_label.setText("未选择 BLF")
                self.blf_status_label.setText("未选择")
            view = data.get("view")
            self._pending_project_view = view if isinstance(view, dict) else None
            self._refresh_current_blf_labels()
            self._refresh_blf_library()
            self._refresh_channel_table()
            self._refresh_replay_header()
        finally:
            self._restoring_workspace = False

    def _restore_project_view(self) -> None:
        view = self._pending_project_view
        self._pending_project_view = None
        if not isinstance(view, dict):
            return
        self.show_signal_titles = bool(view.get("show_signal_titles", self.show_signal_titles))
        self.show_grid = bool(view.get("show_grid", self.show_grid))
        self.title_visibility_action.setChecked(self.show_signal_titles)
        self.grid_visibility_action.setChecked(self.show_grid)
        selected_data = view.get("selected_signals", [])
        selected = [self._signal_id_from_data(item) for item in selected_data] if isinstance(selected_data, list) else []
        for signal_id in selected:
            if signal_id in self.series:
                self._set_signal_tree_checked(signal_id)
                self._add_plot(signal_id)
        time_range = view.get("time_range")
        if self.plots and isinstance(time_range, list) and len(time_range) == 2:
            try:
                next(iter(self.plots.values())).setXRange(float(time_range[0]), float(time_range[1]), padding=0.0)
            except (TypeError, ValueError):
                pass
        self.cursor_enabled = bool(view.get("cursor_enabled", False))
        blue_time = view.get("blue_cursor_time")
        yellow_time = view.get("yellow_cursor_time")
        self.blue_cursor_time = float(blue_time) if isinstance(blue_time, (int, float)) else None
        self.yellow_cursor_time = float(yellow_time) if isinstance(yellow_time, (int, float)) else None
        self._update_cursor_controls()
        self._sync_cursor_lines()
        self._refresh_cursor_details()

    def _set_signal_tree_checked(self, signal_id: SignalId) -> None:
        self.tree.blockSignals(True)
        try:
            for parent_index in range(self.tree.topLevelItemCount()):
                parent = self.tree.topLevelItem(parent_index)
                for child_index in range(parent.childCount()):
                    child = parent.child(child_index)
                    if child.data(0, Qt.ItemDataRole.UserRole) == signal_id:
                        child.setCheckState(0, Qt.CheckState.Checked)
                        return
        finally:
            self.tree.blockSignals(False)

    def _build_actions(self) -> None:
        new_project = QAction("新建工程", self)
        new_project.triggered.connect(self.new_project)
        open_project = QAction("打开工程…", self)
        open_project.triggered.connect(self.open_project)
        save_project = QAction("保存工程", self)
        save_project.triggered.connect(self.save_project)
        save_project_as = QAction("工程另存为…", self)
        save_project_as.triggered.connect(self.save_project_as)
        add_dbc = QAction("\u6dfb\u52a0 DBC", self)
        add_dbc.triggered.connect(self.add_dbc)
        open_blf = QAction("\u6253\u5f00 BLF", self)
        open_blf.triggered.connect(self.open_blf)
        decode = QAction("\u5e94\u7528\u7ed1\u5b9a\u5e76\u89e3\u6790", self)
        decode.triggered.connect(self.apply_bindings_and_decode)
        clear = QAction("\u6e05\u9664\u6ce2\u5f62", self)
        clear.triggered.connect(self.clear_plots)
        menu = self.menuBar().addMenu("\u6587\u4ef6")
        menu.addAction(new_project)
        menu.addAction(open_project)
        menu.addAction(save_project)
        menu.addAction(save_project_as)
        menu.addSeparator()
        menu.addAction(add_dbc)
        menu.addAction(open_blf)
        menu.addAction(decode)
        menu.addSeparator()
        menu.addAction(clear)

        view_menu = self.menuBar().addMenu("\u89c6\u56fe")
        self.toggle_navigation_action = QAction("\u663e\u793a\u5de6\u4fa7\u5de5\u4f5c\u533a", self, checkable=True)
        self.toggle_navigation_action.setChecked(True)
        self.toggle_navigation_action.toggled.connect(self._set_navigation_visible)
        self.toggle_details_action = QAction("\u663e\u793a\u53f3\u4fa7\u4fe1\u606f\u680f", self, checkable=True)
        self.toggle_details_action.setChecked(True)
        self.toggle_details_action.toggled.connect(self._set_details_visible)
        reset_layout = QAction("\u91cd\u7f6e\u5de5\u4f5c\u533a\u5e03\u5c40", self)
        reset_layout.triggered.connect(self.reset_workspace)
        view_menu.addAction(self.toggle_navigation_action)
        view_menu.addAction(self.toggle_details_action)
        view_menu.addSeparator()
        view_menu.addAction(reset_layout)

        graphic_menu = self.menuBar().addMenu("\u6ce2\u5f62")
        fit_graphic = QAction("\u9002\u914d\u5168\u90e8\u6ce2\u5f62", self)
        fit_graphic.triggered.connect(self.fit_graphic)
        reset_time = QAction("\u590d\u4f4d\u65f6\u95f4\u8f74", self)
        reset_time.triggered.connect(self.reset_time_axis)
        zoom_in = QAction("\u65f6\u95f4\u8f74\u653e\u5927", self)
        zoom_in.triggered.connect(lambda: self.zoom_time_axis(0.75))
        zoom_out = QAction("\u65f6\u95f4\u8f74\u7f29\u5c0f", self)
        zoom_out.triggered.connect(lambda: self.zoom_time_axis(1.35))
        self.title_visibility_action = QAction("\u663e\u793a\u4fe1\u53f7\u540d\u79f0", self, checkable=True)
        self.title_visibility_action.setChecked(True)
        self.title_visibility_action.toggled.connect(self.set_signal_titles_visible)
        self.grid_visibility_action = QAction("\u663e\u793a\u7f51\u683c", self, checkable=True)
        self.grid_visibility_action.setChecked(True)
        self.grid_visibility_action.toggled.connect(self.set_grid_visible)
        graphic_menu.addAction(fit_graphic)
        graphic_menu.addAction(reset_time)
        graphic_menu.addAction(zoom_in)
        graphic_menu.addAction(zoom_out)
        graphic_menu.addSeparator()
        graphic_menu.addAction(self.title_visibility_action)
        graphic_menu.addAction(self.grid_visibility_action)

        toolbar = self.addToolBar("\u56de\u653e")
        toolbar.addAction(open_project)
        toolbar.addAction(save_project)
        toolbar.addSeparator()
        toolbar.addAction(add_dbc)
        toolbar.addAction(open_blf)
        toolbar.addAction(decode)
        toolbar.addAction(clear)
        toolbar.addSeparator()
        toolbar.addAction(fit_graphic)
        toolbar.addAction(zoom_in)
        toolbar.addAction(zoom_out)
        toolbar.addAction(self.title_visibility_action)

    def add_dbc(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "\u9009\u62e9 DBC \u6587\u4ef6", "", "DBC Files (*.dbc)")
        if not paths:
            return
        try:
            added = self.decoder.add_dbc_files(paths)
        except Exception as error:
            self._show_error("DBC \u5bfc\u5165\u5931\u8d25", str(error))
            return
        self._refresh_dbc_list()
        self._refresh_channel_table()
        self._save_resource_workspace()
        if added:
            self.statusBar().showMessage(f"\u5df2\u6dfb\u52a0 {len(added)} \u4e2a DBC\uff0c\u5f53\u524d\u5171 {len(self.decoder.dbc_files)} \u4e2a\u3002")
        else:
            self.statusBar().showMessage("\u9009\u62e9\u7684 DBC \u5df2\u5728\u5217\u8868\u4e2d\u3002")

    def remove_selected_dbc(self) -> None:
        row = self.dbc_list.currentRow()
        item = self.dbc_list.item(row, 0) if row >= 0 else None
        if item is None:
            return
        dbc_path = item.data(Qt.ItemDataRole.UserRole)
        self.decoder.remove_dbc_file(dbc_path)
        for channel, bound_path in self.channel_bindings.items():
            if bound_path == dbc_path:
                self.channel_bindings[channel] = None
        self._refresh_dbc_list()
        self._refresh_channel_table()
        self._save_resource_workspace()
        self.statusBar().showMessage("\u5df2\u4ece\u8f6f\u4ef6\u8d44\u6e90\u5217\u8868\u79fb\u9664 DBC\uff08\u672a\u5220\u9664\u539f\u59cb\u6587\u4ef6\uff09\u3002")

    def open_blf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "\u9009\u62e9 CANoe BLF \u65e5\u5fd7", "", "BLF Files (*.blf)")
        if not path:
            return
        dialog = self._progress_dialog("\u6b63\u5728\u626b\u63cf BLF \u901a\u9053\u2026")
        try:
            self.channel_counts = self.decoder.scan_blf_channels(path, self._progress_callback(dialog))
        except Exception as error:
            self._show_error("BLF \u626b\u63cf\u5931\u8d25", str(error))
            return
        finally:
            dialog.close()

        self.current_blf_path = path
        self.channel_bindings = {channel: None for channel in self.channel_counts}
        self.blf_library[path] = {
            "path": path,
            "channels": dict(self.channel_counts),
            "bindings": dict(self.channel_bindings),
            "duration": self.decoder.last_scan_duration,
        }
        self._refresh_current_blf_labels()
        self._clear_decoded_data()
        self._refresh_blf_library()
        self._refresh_channel_table()
        self._refresh_replay_header()
        self._save_resource_workspace()
        self.statusBar().showMessage(f"\u5df2\u626b\u63cf {len(self.channel_counts)} \u4e2a BLF \u901a\u9053\u3002\u8bf7\u4e3a\u9700\u8981\u89e3\u7801\u7684\u901a\u9053\u9009\u62e9 DBC\u3002")

    def apply_bindings_and_decode(self) -> None:
        if self.current_blf_path is None:
            self._show_error("\u672a\u9009\u62e9 BLF", "\u8bf7\u5148\u6253\u5f00 BLF \u5e76\u626b\u63cf\u5176\u901a\u9053\u3002")
            return
        self._read_channel_combos()
        self._save_resource_workspace()
        if not any(self.channel_bindings.values()):
            self._show_error("\u672a\u7ed1\u5b9a DBC", "\u8bf7\u81f3\u5c11\u4e3a\u4e00\u4e2a BLF \u901a\u9053\u9009\u62e9 DBC\u3002")
            return
        dialog = self._progress_dialog("\u6b63\u5728\u6309\u901a\u9053\u7ed1\u5b9a\u89e3\u7801 BLF \u2026")
        try:
            self.series, self.end_time, frames, decoded = self.decoder.decode_blf(
                self.current_blf_path,
                self.channel_bindings,
                self._progress_callback(dialog),
            )
        except Exception as error:
            self._show_error("BLF \u89e3\u7801\u5931\u8d25", str(error))
            return
        finally:
            dialog.close()
        self._populate_tree()
        self._refresh_signal_groups_ui()
        self._restore_project_view()
        self._refresh_replay_header()
        self.last_parse_label.setText(
            f"上次解析结果：{len(self.series):,} 个信号 · {decoded:,} 帧"
        )
        self.navigation_tabs.setCurrentIndex(1)
        self.statusBar().showMessage(
            f"\u89e3\u7801\u5b8c\u6210\uff1a\u626b\u63cf {frames:,} \u5e27\uff0c\u89e3\u7801 {decoded:,} \u5e27\uff0c\u5f97\u5230 {len(self.series):,} \u4e2a\u4fe1\u53f7\u3002"
        )

    def _refresh_dbc_list(self) -> None:
        selected_path = None
        row = self.dbc_list.currentRow()
        if row >= 0 and self.dbc_list.item(row, 0) is not None:
            selected_path = self.dbc_list.item(row, 0).data(Qt.ItemDataRole.UserRole)
        self.dbc_list.setRowCount(len(self.decoder.dbc_files))
        for row, info in enumerate(self.decoder.dbc_files):
            name_item = QTableWidgetItem(info.name)
            name_item.setData(Qt.ItemDataRole.UserRole, info.path)
            name_item.setToolTip(info.path)
            count_item = QTableWidgetItem(f"{info.message_count:,}")
            path_item = QTableWidgetItem(info.path)
            path_item.setToolTip(info.path)
            status_item = QTableWidgetItem("● 可用")
            status_item.setForeground(QColor("#55df8a"))
            for column, item in enumerate((name_item, count_item, path_item, status_item)):
                self.dbc_list.setItem(row, column, item)
            if info.path == selected_path:
                self.dbc_list.selectRow(row)
        self.dbc_summary_label.setText(f"已保存 {len(self.decoder.dbc_files)} 个 DBC")
        self._filter_dbc_table(self.dbc_search.text())

    def _filter_dbc_table(self, text: str) -> None:
        query = text.strip().lower()
        for row in range(self.dbc_list.rowCount()):
            item = self.dbc_list.item(row, 0)
            path = item.data(Qt.ItemDataRole.UserRole) if item is not None else ""
            self.dbc_list.setRowHidden(row, query not in str(path).lower())

    def _refresh_blf_library(self) -> None:
        self.blf_table.blockSignals(True)
        try:
            entries = list(self.blf_library.values())
            self.blf_table.setRowCount(len(entries))
            selected_row = -1
            for row, entry in enumerate(entries):
                path = str(entry["path"])
                file_path = Path(path)
                channels = entry.get("channels", {})
                try:
                    size_text = self._format_file_size(file_path.stat().st_size)
                except OSError:
                    size_text = "—"
                duration_text = self._format_duration(float(entry.get("duration", 0.0)))
                name_item = QTableWidgetItem(file_path.name)
                name_item.setData(Qt.ItemDataRole.UserRole, path)
                name_item.setToolTip(path)
                status_item = QTableWidgetItem("● 已扫描" if file_path.is_file() else "● 文件缺失")
                status_item.setForeground(QColor("#55df8a" if file_path.is_file() else "#e47b7b"))
                values = (
                    name_item,
                    QTableWidgetItem(size_text),
                    QTableWidgetItem(duration_text),
                    QTableWidgetItem(str(len(channels) if isinstance(channels, dict) else 0)),
                    status_item,
                )
                for column, item in enumerate(values):
                    self.blf_table.setItem(row, column, item)
                if path == self.current_blf_path:
                    selected_row = row
            if selected_row >= 0:
                self.blf_table.selectRow(selected_row)
        finally:
            self.blf_table.blockSignals(False)
        self.blf_summary_label.setText(f"已保存 {len(self.blf_library)} 个日志")
        self._filter_blf_table(self.blf_search.text())

    def _filter_blf_table(self, text: str) -> None:
        query = text.strip().lower()
        for row in range(self.blf_table.rowCount()):
            item = self.blf_table.item(row, 0)
            path = item.data(Qt.ItemDataRole.UserRole) if item is not None else ""
            self.blf_table.setRowHidden(row, query not in str(path).lower())

    def _activate_selected_blf(self) -> None:
        row = self.blf_table.currentRow()
        item = self.blf_table.item(row, 0) if row >= 0 else None
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        entry = self.blf_library.get(path)
        if entry is None or not Path(path).is_file() or path == self.current_blf_path:
            return
        self.current_blf_path = path
        channels = entry.get("channels", {})
        self.channel_counts = {int(channel): int(count) for channel, count in channels.items()}
        available_paths = {info.path for info in self.decoder.dbc_files}
        saved_bindings = entry.get("bindings", {})
        self.channel_bindings = {
            channel: saved_bindings.get(channel) if saved_bindings.get(channel) in available_paths else None
            for channel in self.channel_counts
        }
        self._clear_decoded_data()
        self._refresh_current_blf_labels()
        self._refresh_channel_table()
        self._refresh_replay_header()
        self._save_resource_workspace()

    def remove_selected_blf(self) -> None:
        row = self.blf_table.currentRow()
        item = self.blf_table.item(row, 0) if row >= 0 else None
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        self.blf_library.pop(path, None)
        if path == self.current_blf_path:
            self.current_blf_path = None
            self.channel_counts = {}
            self.channel_bindings = {}
            self._clear_decoded_data()
            self._refresh_current_blf_labels()
            self._refresh_channel_table()
            self._refresh_replay_header()
        self._refresh_blf_library()
        self._save_resource_workspace()

    @staticmethod
    def _format_file_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024.0 or unit == "GB":
                return f"{value:.2f} {unit}"
            value /= 1024.0
        return f"{value:.2f} GB"

    @staticmethod
    def _format_duration(duration: float) -> str:
        seconds = max(0, int(round(duration)))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _refresh_channel_table(self) -> None:
        self._channel_combos.clear()
        self.channel_table.setRowCount(len(self.channel_counts))
        for row, (channel, frame_count) in enumerate(sorted(self.channel_counts.items())):
            channel_item = QTableWidgetItem(f"CH{channel}")
            channel_item.setData(Qt.ItemDataRole.UserRole, channel)
            self.channel_table.setItem(row, 0, channel_item)
            self.channel_table.setItem(row, 1, QTableWidgetItem(f"{frame_count:,}"))
            combo = QComboBox()
            combo.addItem("\u672a\u7ed1\u5b9a\uff08\u8df3\u8fc7\u6b64\u901a\u9053\uff09", None)
            for info in self.decoder.dbc_files:
                combo.addItem(f"{info.name}  ({info.message_count} \u6761\u62a5\u6587)", info.path)
            current_path = self.channel_bindings.get(channel)
            if current_path is not None:
                index = combo.findData(current_path)
                if index >= 0:
                    combo.setCurrentIndex(index)
            combo.currentIndexChanged.connect(lambda _index, ch=channel: self._on_binding_changed(ch))
            self.channel_table.setCellWidget(row, 2, combo)
            self._channel_combos[channel] = combo
            self._set_channel_status(row, current_path is not None)
        self._update_binding_controls()

    def _on_binding_changed(self, channel: int) -> None:
        combo = self._channel_combos.get(channel)
        if combo is not None:
            self.channel_bindings[channel] = combo.currentData()
        for row in range(self.channel_table.rowCount()):
            item = self.channel_table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == channel:
                self._set_channel_status(row, self.channel_bindings.get(channel) is not None)
                break
        if self.series:
            self._clear_decoded_data()
        if self.current_blf_path in self.blf_library:
            self.blf_library[self.current_blf_path]["bindings"] = dict(self.channel_bindings)
        self._update_binding_controls()
        self._refresh_replay_header()
        self._save_resource_workspace()

    def _focus_binding_table(self) -> None:
        self.channel_table.setFocus()

    def clear_all_bindings(self) -> None:
        for combo in self._channel_combos.values():
            combo.setCurrentIndex(0)

    def _refresh_current_blf_labels(self) -> None:
        if self.current_blf_path is None:
            self.blf_label.setText("未选择 BLF")
            self.blf_label.setToolTip("")
            self.blf_status_label.setText("未选择")
            self.binding_blf_label.setText("当前 BLF：未选择")
            self.parse_blf_label.setText("当前 BLF\n未选择")
            return
        name = Path(self.current_blf_path).name
        self.blf_label.setText(f"当前使用：{name}")
        self.blf_label.setToolTip(self.current_blf_path)
        self.blf_status_label.setText(f"已扫描 {len(self.channel_counts)} 个通道")
        self.binding_blf_label.setText(f"当前 BLF：{name}\n检测到 {len(self.channel_counts)} 个通道")
        entry = self.blf_library.get(self.current_blf_path, {})
        try:
            size_text = self._format_file_size(Path(self.current_blf_path).stat().st_size)
        except OSError:
            size_text = "—"
        duration_text = self._format_duration(float(entry.get("duration", 0.0)))
        self.parse_blf_label.setText(f"当前 BLF\n{name}\n{size_text}  |  {duration_text}")

    def _set_channel_status(self, row: int, bound: bool) -> None:
        status = QTableWidgetItem("\u5df2\u7ed1\u5b9a" if bound else "\u672a\u53c2\u4e0e")
        status.setForeground(QColor("#55df8a" if bound else "#8d99a4"))
        self.channel_table.setItem(row, 3, status)

    def _update_binding_controls(self) -> None:
        bound_count = sum(path is not None for path in self.channel_bindings.values())
        skipped_count = max(0, len(self.channel_counts) - bound_count)
        ready = self.current_blf_path is not None and bound_count > 0
        self.apply_button.setEnabled(ready)
        if bound_count:
            self.binding_ready_label.setText(
                f"\u5df2\u5c31\u7eea\uff1a{bound_count} \u4e2a\u901a\u9053\u53c2\u4e0e\u89e3\u6790\uff1b"
                f"{skipped_count} \u4e2a\u672a\u7ed1\u5b9a\u901a\u9053\u5c06\u8df3\u8fc7"
            )
        else:
            self.binding_ready_label.setText("\u8bf7\u81f3\u5c11\u7ed1\u5b9a\u4e00\u4e2a\u901a\u9053")
        bound_lines = [
            f"CH{channel} → {Path(path).name}"
            for channel, path in sorted(self.channel_bindings.items())
            if path is not None
        ]
        skipped_lines = [
            f"CH{channel}（未绑定）"
            for channel, path in sorted(self.channel_bindings.items())
            if path is None
        ]
        self.parse_bound_label.setText("参与解析\n" + ("\n".join(bound_lines) if bound_lines else "暂无已绑定通道"))
        self.parse_skipped_label.setText("跳过通道\n" + ("\n".join(skipped_lines) if skipped_lines else "暂无"))
        estimated_frames = sum(
            self.channel_counts.get(channel, 0)
            for channel, path in self.channel_bindings.items()
            if path is not None
        )
        self.parse_summary_label.setText(
            f"扫描通道：{len(self.channel_counts)}    参与通道：{bound_count}    预计报文：{estimated_frames:,}"
        )

    def _read_channel_combos(self) -> None:
        for channel, combo in self._channel_combos.items():
            self.channel_bindings[channel] = combo.currentData()

    def _refresh_replay_header(self) -> None:
        if self.current_blf_path is None:
            self._set_replay_header("\u5f53\u524d\u672a\u9009\u62e9 BLF \u65e5\u5fd7")
            self.binding_details.setText("\u6682\u65e0\u7ed1\u5b9a")
            return
        bindings = []
        for channel, dbc_path in sorted(self.channel_bindings.items()):
            if dbc_path is not None:
                bindings.append(f"CH{channel} \u2192 {Path(dbc_path).name}")
        mapping_text = "\uff1b".join(bindings) if bindings else "\u6682\u672a\u7ed1\u5b9a DBC"
        self._set_replay_header(f"BLF\uff1a{Path(self.current_blf_path).name}    |    {mapping_text}")
        self.binding_details.setText(mapping_text.replace("\uff1b", "\n"))

    def _set_replay_header(self, text: str) -> None:
        self._replay_header_text = text
        self.replay_header.setToolTip(text)
        self._update_replay_header_elision()

    def _update_replay_header_elision(self) -> None:
        available_width = max(1, self.replay_header.contentsRect().width())
        text = QFontMetrics(self.replay_header.font()).elidedText(
            self._replay_header_text,
            Qt.TextElideMode.ElideMiddle,
            available_width,
        )
        self.replay_header.setText(text)

    def _save_resource_workspace(self) -> None:
        if self._restoring_workspace:
            return
        blf_data: dict[str, object] | None = None
        if self.current_blf_path is not None:
            if self.current_blf_path in self.blf_library:
                self.blf_library[self.current_blf_path]["channels"] = dict(self.channel_counts)
                self.blf_library[self.current_blf_path]["bindings"] = dict(self.channel_bindings)
            blf_data = {
                "path": self.current_blf_path,
                "channels": {str(channel): count for channel, count in self.channel_counts.items()},
                "bindings": {str(channel): path for channel, path in self.channel_bindings.items()},
                "signature": self._file_signature(self.current_blf_path),
            }
        workspace = {
            "version": 1,
            "dbc_paths": [info.path for info in self.decoder.dbc_files],
            "blf": blf_data,
            "blf_library": [
                {
                    "path": entry["path"],
                    "channels": {str(channel): count for channel, count in entry.get("channels", {}).items()},
                    "bindings": {str(channel): path for channel, path in entry.get("bindings", {}).items()},
                    "duration": entry.get("duration", 0.0),
                }
                for entry in self.blf_library.values()
            ],
        }
        try:
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            temporary_file = self.workspace_file.with_suffix(".tmp")
            temporary_file.write_text(
                json.dumps(workspace, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_file.replace(self.workspace_file)
        except OSError as error:
            self.statusBar().showMessage(f"\u65e0\u6cd5\u4fdd\u5b58\u5de5\u4f5c\u533a\u914d\u7f6e\uff1a{error}")

    def _restore_resource_workspace(self) -> None:
        if not self.workspace_file.exists():
            return
        try:
            workspace = json.loads(self.workspace_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.statusBar().showMessage("\u5de5\u4f5c\u533a\u914d\u7f6e\u6587\u4ef6\u65e0\u6cd5\u8bfb\u53d6\uff0c\u5df2\u4ee5\u7a7a\u914d\u7f6e\u542f\u52a8\u3002")
            return

        self._restoring_workspace = True
        try:
            dbc_paths = [path for path in workspace.get("dbc_paths", []) if Path(path).is_file()]
            if dbc_paths:
                self.decoder.add_dbc_files(dbc_paths)
            self._refresh_dbc_list()

            self.blf_library = {}
            library_data = workspace.get("blf_library", [])
            if isinstance(library_data, list):
                for saved_entry in library_data:
                    if not isinstance(saved_entry, dict):
                        continue
                    path = saved_entry.get("path")
                    channels = saved_entry.get("channels", {})
                    bindings = saved_entry.get("bindings", {})
                    if not isinstance(path, str) or not Path(path).is_file() or not isinstance(channels, dict):
                        continue
                    self.blf_library[path] = {
                        "path": path,
                        "channels": {int(channel): int(count) for channel, count in channels.items()},
                        "bindings": {
                            int(channel): bound_path
                            for channel, bound_path in bindings.items()
                            if isinstance(bound_path, str)
                        } if isinstance(bindings, dict) else {},
                        "duration": float(saved_entry.get("duration", 0.0)),
                    }
            self._refresh_blf_library()

            blf_data = workspace.get("blf")
            if not isinstance(blf_data, dict):
                self._refresh_current_blf_labels()
                return
            blf_path = blf_data.get("path")
            if not isinstance(blf_path, str) or not Path(blf_path).is_file():
                self.statusBar().showMessage("\u4e0a\u6b21\u4f7f\u7528\u7684 BLF \u6587\u4ef6\u4e0d\u5b58\u5728\uff0c\u8bf7\u91cd\u65b0\u6253\u5f00 BLF\u3002")
                return

            saved_channels = blf_data.get("channels", {})
            saved_bindings = blf_data.get("bindings", {})
            if not isinstance(saved_channels, dict) or not isinstance(saved_bindings, dict):
                return
            self.current_blf_path = blf_path
            self.channel_counts = {int(channel): int(count) for channel, count in saved_channels.items()}
            available_paths = {info.path for info in self.decoder.dbc_files}
            self.channel_bindings = {
                channel: saved_bindings.get(str(channel)) if saved_bindings.get(str(channel)) in available_paths else None
                for channel in self.channel_counts
            }
            if blf_path not in self.blf_library:
                self.blf_library[blf_path] = {
                    "path": blf_path,
                    "channels": dict(self.channel_counts),
                    "bindings": dict(self.channel_bindings),
                    "duration": 0.0,
                }
            self._refresh_current_blf_labels()
            self._refresh_blf_library()
            self._refresh_channel_table()
            self._refresh_replay_header()
            self.statusBar().showMessage("\u5df2\u6062\u590d\u4e0a\u6b21\u7684 DBC\u3001BLF \u548c\u901a\u9053\u7ed1\u5b9a\u3002\u8bf7\u624b\u52a8\u70b9\u51fb\u201c\u89e3\u6790\u5f53\u524d\u65e5\u5fd7\u201d\u3002")
        finally:
            self._restoring_workspace = False

    @staticmethod
    def _file_signature(path: str) -> dict[str, int] | None:
        try:
            stats = Path(path).stat()
        except OSError:
            return None
        return {"size": stats.st_size, "modified_ns": stats.st_mtime_ns}

    def _show_startup_hint(self) -> None:
        if not self.settings.value("show_startup_hint", True, type=bool):
            return
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("\u4f7f\u7528\u63d0\u793a")
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setText("\u4f7f\u7528\u6b65\u9aa4")
        dialog.setInformativeText(
            "1. \u6dfb\u52a0 DBC\n"
            "2. \u6253\u5f00 BLF\n"
            "3. \u4e3a\u6bcf\u4e2a BLF \u901a\u9053\u9009\u62e9 DBC\n"
            "4. \u70b9\u51fb\u201c\u5e94\u7528\u7ed1\u5b9a\u5e76\u89e3\u6790\u201d"
        )
        show_again = QCheckBox("\u542f\u52a8\u65f6\u663e\u793a\u6b64\u63d0\u793a")
        show_again.setChecked(True)
        dialog.setCheckBox(show_again)
        dialog.addButton("\u77e5\u9053\u4e86", QMessageBox.ButtonRole.AcceptRole)
        dialog.exec()
        self.settings.setValue("show_startup_hint", show_again.isChecked())
        self.settings.sync()

    def _progress_dialog(self, title: str) -> QProgressDialog:
        dialog = QProgressDialog(title, "", 0, 100, self)
        dialog.setWindowTitle("CANQT")
        dialog.setCancelButton(None)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.show()
        return dialog

    @staticmethod
    def _progress_callback(dialog: QProgressDialog):
        def update(percent: int, message: str) -> None:
            dialog.setValue(percent)
            dialog.setLabelText(message)
            QApplication.processEvents()
        return update

    def _clear_decoded_data(self) -> None:
        self.series.clear()
        self.end_time = 0.0
        self.tree.clear()
        self.clear_plots()
        self.clear_cursors()
        self._refresh_signal_groups_ui()

    def _populate_tree(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        messages: dict[tuple[str, int, int | None, bool], list[SignalSeries]] = {}
        for item in self.series.values():
            key = (
                item.signal_id.message_name,
                item.signal_id.channel or 0,
                item.signal_id.frame_id,
                item.signal_id.is_extended_frame,
            )
            messages.setdefault(key, []).append(item)
        for (message_name, channel, frame_id, is_extended_frame), items in sorted(messages.items()):
            parent = QTreeWidgetItem([
                message_name,
                self._format_frame_id(frame_id, is_extended_frame),
                "",
                f"CH{channel}",
            ])
            parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self.tree.addTopLevelItem(parent)
            for item in sorted(items, key=lambda value: value.signal_id.signal_name):
                child = QTreeWidgetItem([item.signal_id.signal_name, "", item.unit, ""])
                child.setData(0, Qt.ItemDataRole.UserRole, item.signal_id)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Unchecked)
                parent.addChild(child)
        self.tree.blockSignals(False)
        self._tree_expansion_before_search = None
        self.tree.collapseAll()

    @staticmethod
    def _format_frame_id(frame_id: int | None, is_extended_frame: bool) -> str:
        if frame_id is None:
            return ""
        suffix = " EXT" if is_extended_frame else ""
        return f"0x{frame_id:X}{suffix}"

    def _filter_tree(self, text: str) -> None:
        query = text.strip().lower()
        if query and self._tree_expansion_before_search is None:
            self._tree_expansion_before_search = {
                (parent.text(0), parent.text(1), parent.text(3)): parent.isExpanded()
                for index in range(self.tree.topLevelItemCount())
                for parent in (self.tree.topLevelItem(index),)
            }
        for index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(index)
            parent_matches = query in parent.text(0).lower()
            visible_children = 0
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                visible = not query or parent_matches or query in child.text(0).lower()
                child.setHidden(not visible)
                visible_children += int(visible)
            parent.setHidden(bool(query) and not parent_matches and visible_children == 0)
            if query and visible_children:
                parent.setExpanded(True)
        if not query and self._tree_expansion_before_search is not None:
            for index in range(self.tree.topLevelItemCount()):
                parent = self.tree.topLevelItem(index)
                key = (parent.text(0), parent.text(1), parent.text(3))
                parent.setExpanded(self._tree_expansion_before_search.get(key, False))
            self._tree_expansion_before_search = None

    def _on_tree_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        signal_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(signal_id, SignalId):
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            self._add_plot(signal_id)
        else:
            self._remove_plot(signal_id)

    def _add_plot(self, signal_id: SignalId) -> None:
        if signal_id in self.plots:
            return
        series = self.series[signal_id]
        color = self.COLORS[len(self.plots) % len(self.COLORS)]
        plot = SignalPlot(series, self.end_time, color, self.show_signal_titles)
        plot.set_grid_visible(self.show_grid)
        plot.blue_cursor_moved.connect(self._on_blue_cursor_moved)
        plot.yellow_cursor_moved.connect(self._on_yellow_cursor_moved)
        plot.cursor_placement_requested.connect(self._on_cursor_placement_requested)
        if self.plots:
            plot.setXLink(next(iter(self.plots.values())))
        self.plots[signal_id] = plot
        self.plot_splitter.addWidget(plot)
        self._update_time_axis_visibility()
        self.reset_time_axis()
        self._sync_cursor_lines()
        self._refresh_cursor_details()
        self._update_plot_area_minimum_height()
        self._refresh_graphic_header()
        self.scroll.show()
        QTimer.singleShot(0, self._distribute_plot_space)

    def _remove_plot(self, signal_id: SignalId) -> None:
        plot = self.plots.pop(signal_id, None)
        if plot is None:
            return
        plot.setParent(None)
        plot.deleteLater()
        self._update_time_axis_visibility()
        self._refresh_cursor_details()
        self._update_plot_area_minimum_height()
        self._refresh_graphic_header()
        if not self.plots:
            self.scroll.hide()
        else:
            QTimer.singleShot(0, self._distribute_plot_space)

    def _update_plot_area_minimum_height(self) -> None:
        count = sum(not plot.isHidden() for plot in self.plots.values())
        handle_height = max(0, count - 1) * self.plot_splitter.handleWidth()
        self.plot_splitter.setMinimumHeight(max(1, count * 100 + handle_height))

    def _distribute_plot_space(self) -> None:
        count = sum(not plot.isHidden() for plot in self.plots.values())
        if count == 0:
            return
        available_height = max(
            self.scroll.viewport().height(),
            count * 100 + max(0, count - 1) * self.plot_splitter.handleWidth(),
        )
        plot_height = available_height // count
        self.plot_splitter.setSizes([
            plot_height if not plot.isHidden() else 0 for plot in self.plots.values()
        ])

    def _update_time_axis_visibility(self) -> None:
        plot_list = [plot for plot in self.plots.values() if not plot.isHidden()]
        for index, plot in enumerate(plot_list):
            plot.set_time_axis_visible(index == len(plot_list) - 1)

    def _refresh_graphic_header(self) -> None:
        self.graphic_header.setText(f"\u5df2\u9009\u62e9 {len(self.plots)} \u4e2a\u4fe1\u53f7")
        self._refresh_selected_signals_ui()

    def set_signal_titles_visible(self, visible: bool) -> None:
        self.show_signal_titles = visible
        for plot in self.plots.values():
            plot.set_title_visible(visible)

    def set_grid_visible(self, visible: bool) -> None:
        self.show_grid = visible
        for plot in self.plots.values():
            plot.set_grid_visible(visible)

    def reset_time_axis(self) -> None:
        if not self.plots:
            return
        first_plot = next(iter(self.plots.values()))
        first_plot.setXRange(0.0, max(self.end_time, 1.0), padding=0.0)

    def zoom_time_axis(self, factor: float) -> None:
        if not self.plots:
            return
        first_plot = next(iter(self.plots.values()))
        first_plot.getPlotItem().getViewBox().scaleBy((factor, 1.0))

    def fit_graphic(self) -> None:
        if not self.plots:
            return
        for plot in self.plots.values():
            plot.getPlotItem().getViewBox().autoRange()
        self.reset_time_axis()

    def clear_plots(self) -> None:
        for signal_id in list(self.plots):
            self._remove_plot(signal_id)
        self.tree.blockSignals(True)
        for index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(index)
            for child_index in range(parent.childCount()):
                parent.child(child_index).setCheckState(0, Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)

    def set_cursor_enabled(self, enabled: bool) -> None:
        self.cursor_enabled = enabled
        self.cursor_placement_mode = None
        self._update_cursor_controls()
        self._sync_cursor_lines()
        self._refresh_cursor_details()

    def set_cursor_placement_mode(self, mode: str) -> None:
        if not self.cursor_enabled:
            return
        self.cursor_placement_mode = None if self.cursor_placement_mode == mode else mode
        self._update_cursor_controls()

    def _update_cursor_controls(self) -> None:
        self.cursor_enable_button.blockSignals(True)
        self.cursor_enable_button.setChecked(self.cursor_enabled)
        self.cursor_enable_button.setText("\u6e38\u6807\uff1a\u5f00\u542f" if self.cursor_enabled else "\u6e38\u6807\uff1a\u5173\u95ed")
        self.cursor_enable_button.blockSignals(False)
        self.place_blue_button.setEnabled(self.cursor_enabled)
        self.place_yellow_button.setEnabled(self.cursor_enabled)
        self.clear_cursors_button.setEnabled(self.cursor_enabled)
        for button, mode in ((self.place_blue_button, "blue"), (self.place_yellow_button, "yellow")):
            button.blockSignals(True)
            button.setChecked(self.cursor_placement_mode == mode)
            button.blockSignals(False)

    def _on_cursor_placement_requested(self, timestamp: float) -> None:
        if not self.cursor_enabled or self.cursor_placement_mode is None:
            return
        timestamp = max(0.0, min(timestamp, self.end_time))
        if self.cursor_placement_mode == "blue":
            self.blue_cursor_time = timestamp
        else:
            self.yellow_cursor_time = timestamp
        self.cursor_placement_mode = None
        self._update_cursor_controls()
        self._queue_cursor_update()

    def _on_blue_cursor_moved(self, timestamp: float) -> None:
        if self.cursor_enabled:
            self.blue_cursor_time = max(0.0, min(timestamp, self.end_time))
            self._queue_cursor_update()

    def _on_yellow_cursor_moved(self, timestamp: float) -> None:
        if self.cursor_enabled:
            self.yellow_cursor_time = max(0.0, min(timestamp, self.end_time))
            self._queue_cursor_update()

    def _queue_cursor_update(self) -> None:
        if not self._cursor_update_timer.isActive():
            self._cursor_update_timer.start(40)

    def _apply_cursor_update(self) -> None:
        self._sync_cursor_lines()
        self._refresh_cursor_details()

    def _sync_cursor_lines(self) -> None:
        for index, plot in enumerate(self.plots.values()):
            plot.set_cursor_times(
                self.blue_cursor_time if self.cursor_enabled else None,
                self.yellow_cursor_time if self.cursor_enabled else None,
                index == 0,
            )

    def clear_cursors(self) -> None:
        self.blue_cursor_time = None
        self.yellow_cursor_time = None
        self.cursor_placement_mode = None
        self._cursor_update_timer.stop()
        self._update_cursor_controls()
        self._sync_cursor_lines()
        self._refresh_cursor_details()

    @staticmethod
    def _series_value_at(series: SignalSeries, timestamp: float | None) -> float | None:
        if timestamp is None:
            return None
        index = bisect.bisect_right(series.timestamps, timestamp) - 1
        return series.values[index] if index >= 0 else None

    @staticmethod
    def _table_item(text: str, color: str | None = None) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if color is not None:
            item.setForeground(QColor(color))
        return item

    def _refresh_cursor_details(self) -> None:
        if not self.cursor_enabled:
            self.cursor_summary.setText("\u6e38\u6807\u672a\u542f\u7528")
            self.cursor_table.setRowCount(0)
            return
        if self.blue_cursor_time is None and self.yellow_cursor_time is None:
            self.cursor_summary.setText("\u8bf7\u9009\u62e9\u201c\u653e\u7f6e\u84dd\u6e38\u6807\u201d\u6216\u201c\u653e\u7f6e\u9ec4\u6e38\u6807\u201d\uff0c\u7136\u540e\u5728\u6ce2\u5f62\u4e0a\u5355\u51fb\u3002")
            self.cursor_table.setRowCount(0)
            return
        summary = []
        if self.blue_cursor_time is not None:
            summary.append(
                f'<span style="color:#22bfff">\u84dd\uff1at = {format_relative_time(self.blue_cursor_time)}</span>'
            )
        if self.yellow_cursor_time is not None:
            yellow = f'<span style="color:#ffd426">\u9ec4\uff1at = {format_relative_time(self.yellow_cursor_time)}</span>'
            if self.blue_cursor_time is not None:
                yellow += f'&nbsp;&nbsp;\u0394t = {format_relative_time(self.yellow_cursor_time - self.blue_cursor_time, include_sign=True)}'
            summary.append(yellow)
        self.cursor_summary.setText("<br>".join(summary))
        self.cursor_table.setRowCount(len(self.plots))
        for row, plot in enumerate(self.plots.values()):
            series = plot.series
            blue_value = self._series_value_at(series, self.blue_cursor_time)
            yellow_value = self._series_value_at(series, self.yellow_cursor_time)
            delta_text = "—"
            if blue_value is not None and yellow_value is not None:
                unit = f" {series.unit}" if series.unit else ""
                delta_text = f"{yellow_value - blue_value:+g}{unit}"
            signal_name = f"{series.signal_id.message_name}.{series.signal_id.signal_name}"
            self.cursor_table.setItem(row, 0, self._table_item(signal_name))
            self.cursor_table.setItem(
                row, 1, self._table_item("—" if blue_value is None else series.format_value(blue_value), "#22bfff")
            )
            self.cursor_table.setItem(
                row, 2, self._table_item("—" if yellow_value is None else series.format_value(yellow_value), "#ffd426")
            )
            self.cursor_table.setItem(row, 3, self._table_item(delta_text))
        self.statusBar().showMessage("\u53cc\u6e38\u6807\u5df2\u66f4\u65b0\uff1a\u9ec4\u503c \u2212 \u84dd\u503c\u3002")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_replay_header_elision()

    def reset_workspace(self) -> None:
        self.expand_navigation()
        self.expand_details()
        self.main_splitter.setSizes([390, 1000, 300])
        if self.plots:
            self._distribute_plot_space()
        self.statusBar().showMessage("\u5de5\u4f5c\u533a\u5e03\u5c40\u5df2\u6062\u590d\u9ed8\u8ba4\u3002")

    def _restore_workspace(self) -> None:
        geometry = self.settings.value("window_geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        navigation_visible = self.settings.value("navigation_visible", True, type=bool)
        details_visible = self.settings.value("details_visible", True, type=bool)
        self._navigation_last_width = self.settings.value(
            "navigation_last_width", self._navigation_last_width, type=int
        )
        self._details_last_width = self.settings.value(
            "details_last_width", self._details_last_width, type=int
        )
        sizes = self.settings.value("main_splitter_sizes")
        if sizes:
            self.main_splitter.setSizes([int(size) for size in sizes])
        self._set_navigation_visible(navigation_visible)
        self._set_details_visible(details_visible)
        self._set_action_checked(self.toggle_navigation_action, navigation_visible)
        self._set_action_checked(self.toggle_details_action, details_visible)
        self.show_signal_titles = self.settings.value("show_signal_titles", True, type=bool)
        self.show_grid = self.settings.value("show_grid", True, type=bool)
        self.title_visibility_action.setChecked(self.show_signal_titles)
        self.grid_visibility_action.setChecked(self.show_grid)
        self._restore_resource_workspace()
        QTimer.singleShot(0, self._show_startup_hint)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.settings.setValue("window_geometry", self.saveGeometry())
        self.settings.setValue("main_splitter_sizes", self.main_splitter.sizes())
        self.settings.setValue("navigation_visible", not self._navigation_collapsed)
        self.settings.setValue("details_visible", not self._details_collapsed)
        self.settings.setValue("navigation_last_width", self._navigation_last_width)
        self.settings.setValue("details_last_width", self._details_last_width)
        self.settings.setValue("show_signal_titles", self.show_signal_titles)
        self.settings.setValue("show_grid", self.show_grid)
        self.settings.sync()
        self._save_resource_workspace()
        super().closeEvent(event)

    def _show_error(self, title: str, details: str) -> None:
        QMessageBox.critical(self, title, details)


def main() -> int:
    pg.setConfigOptions(antialias=False)
    application = QApplication(sys.argv)
    application.setApplicationName("CANQT")
    application.setWindowIcon(QIcon(str(APP_ICON)))
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
