# WiFiApple

A Flask web server that uses the MLB Stats API to detect base hits, home runs, and wins for the team you choose to monitor, and sends an Arduino trigger upon detection. 


# Disclaimer: This was 99% "vibe coded" by chatGPT. A ton of research, time and tweaking went into it, but I'd be lying if I said I "coded this myself." I did not. As such, it's probably a pretty awful implementation, but hey, it works!


#Description

This is a horribly inefficient local web server that monitors the team you set (defaults to the Mets) and sends an Arduino trigger event when it detects a base hit, home run, or Win. For my purposes, this was set up to trigger a linear actuator, controlled by an Arduino Nano ESP32, which would raise and lower my own Home Run Apple that I could keep on my desk. 

It notes the start time of the server so as to not accidentally trigger for any hits that may have occurred prior to the start of monitoring.

Determines home and away teams via teamID and HalfInning so as to only detect hits from the team you are monitoring.

Has checks for doubleheaders, postponed games, delayed games, etc. It should reliably (and almost immediately) find the correct gameID for the current or upcoming game.

When the game status changes from "In-Progress" to "Game Over," the script will find the final score and determine if the monitored team won. If it did, a trigger will be sent. 


To use: install Flask and (https://github.com/toddrob99/MLB-StatsAPI)[the MLB Stats API from Todd Rob]

run the script from the command line. it will launch a web page on localhost:5000

Use the web page to select the team of your choice from the dropdown menu. Alternatively, you can just change the default team in the script, using the big ol list of teams and their ID's contained within the script itself. 

you can monitor the script's activity via the command line. 
