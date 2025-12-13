QT += core gui widgets network

CONFIG += c++17 app_bundle

TARGET = StarBridge
TEMPLATE = app

# App icon
ICON = resources/StarBridge.icns

# Resources
RESOURCES += resources.qrc

SOURCES += \
    main.cpp \
    LauncherWindow.cpp \
    ProcessRunner.cpp

HEADERS += \
    LauncherWindow.h \
    ProcessRunner.h

# Deployment
macx {
    QMAKE_INFO_PLIST = Info.plist
    APP_ICONS.files = resources/StarBridge.icns
    QMAKE_BUNDLE_DATA += APP_ICONS
}
