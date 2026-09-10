"""Size the selected page, not the largest hidden page in the stack."""

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QStackedWidget


class AdaptiveStack(QStackedWidget):
    def sizeHint(self):
        current = self.currentWidget()
        return current.sizeHint() if current else QSize(0, 0)

    def minimumSizeHint(self):
        current = self.currentWidget()
        return current.minimumSizeHint() if current else QSize(0, 0)

    def hasHeightForWidth(self):
        # QStackedLayout includes hidden pages in its height-for-width cache.
        # Use the active page's ordinary size hints to avoid a tall Library
        # stretching the Classes form after navigation.
        return False

    def heightForWidth(self, width):
        return -1
