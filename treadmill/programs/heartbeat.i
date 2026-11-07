; heartbeat.i: tick the bottom-right screen cell once per frame, forever.
; A test program ends with this so the video channel can tell a running
; program from one that hung: the cell at row 24, column 39 ($07E7) changes
; every frame while the 6510 is alive, whatever the rest of the screen does.
; Raster line $FF comes once per frame on PAL and NTSC alike.

heartbeat:
-   lda $d012
    cmp #$ff
    bne -
    inc $07e7
-   lda $d012
    cmp #$ff
    beq -
    jmp heartbeat
