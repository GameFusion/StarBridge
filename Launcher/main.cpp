#include <QApplication>
#include <QSplashScreen>
#include <QTimer>
#include "LauncherWindow.h"

int main(int argc, char *argv[])
{
    QApplication app(argc, argv);
    app.setApplicationName("StarBridge");
    app.setApplicationVersion("1.0");
    app.setOrganizationName("StarGit Studio");

    // === ELITE SPLASH SCREEN ===
    QPixmap splashPixmap(":/resources/splash.png");
    QSplashScreen splash(splashPixmap, Qt::WindowStaysOnTopHint);
    splash.show();
    app.processEvents();

    // === MAIN WINDOW ===
    LauncherWindow window;
    window.show();

    // Close splash after 2 seconds
    QTimer::singleShot(2000, &splash, &QSplashScreen::close);

    return app.exec();
}
