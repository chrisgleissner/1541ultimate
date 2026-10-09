;-----------------------------------------------------------------------
; FILE keyboard.asm
;
; Written by Wilfred Bos
;
; Copyright (c) 2009 - 2019 Wilfred Bos / Gideon Zweijtzer
;
; DESCRIPTION
;   Routines for handling key presses for changing song, fast forward
;   and for returning to Ultimate menu.
;
;   The following keys are supported:
;     <- for fast forward, faster the longer it is held
;     , for rewind by 10 seconds, by more the longer it is held
;     CRSR right and CRSR left (SHIFT + CRSR) like <- and ,
;     1-0 for selecting sub tune 1 to 10
;     + and - for increasing/decreasing the song selection
;     runstop for going back to Ultimate menu
;     space for pausing/resuming playback
;
; Use 64tass version 1.53.1515 or higher to assemble the code
;-----------------------------------------------------------------------

handleKeyboard  lda #$fe            ; CRSR left/right is in row 1, which the loop below skips
                sta $dc00
                lda $dc01
                and #$04
                bne noCursor
                jmp cursorKey
noCursor        lda #$00
                sta cursorHeld

                ldx #7
-               ldy keyRow,x
                sty $dc00
                lda $dc01
                cmp #$ff
                bne keyPressed
                sta currentKey,x
                dex
                bne -               ; ignore row 1 (x = 0) since those keys are not supported for now

.if INCLUDE_RUNSTOP==1
                lda runStopPressed  ; check if runstop key is pressed and released
                beq +
                lda #$00
                sta runStopPressed
                jmp gotoUltimateMenu
+
.fi
                jsr releaseTurbo
                ldy #0
                jmp fastForward

keyPressed      cmp currentKey,x
                bne keyChanged
                jmp keyHeld
keyChanged      sta currentKey,x

                cpx #$06            ; check for row 7 (which is not handled)
                beq skipKeyCheck

                cpx #$01
                bne +
                jsr handleRow2
+
                cpx #$05
                beq handleRow6
                cpx #$07            ; check for runstop to exit player, <- key to fastforward tune and space to pause
                bne checkNumKeys

.if INCLUDE_RUNSTOP==1
                cmp #$7f            ; check if runstop key is pressed
                bne noRunStop
                lda #$01
                sta runStopPressed
noRunStop
.fi
                ldy cantPause
                bne checkOtherKeys

                cmp #$ef            ; check if space key is pressed
                bne checkOtherKeys

                lda pauseTune
                sta $d418           ; toggle volume off
sid2            sta $d418
sid3            sta $d418
                eor #$1f
                sta pauseTune
pause           sta @w $0000
notReleased     rts

checkOtherKeys  cmp #$fd
                bne checkNumKeys
startForward    ldy #$00
                sty holdTicks
                iny
fastForward     sty @w $0000
                lda clockRates,y    ; the clock counts play calls while fast forwarding
                sta clock.framesPerSec
skipKeyCheck    rts

checkNumKeys    ldy pauseTune
                bne skipKeyCheck

                ; handle keys 0-9
                tay
                and #$7f
                cmp #$7e
                beq +
                cmp #$77
                bne skipKeyCheck
+
                txa
                asl
                tax
                tya
                lsr
                lda #$00
                adc tuneSelect,x
                cmp maxSong
                beq setCurrentSong
                bcs skipKeyCheck
                jmp setCurrentSong

handleRow2      pha
                and #$7f
                cmp #$5f            ; check for S and shift-S key
                bne +
                lda $d011           ; toggle screen on/off
                eor #$10
                sta $d011
+               pla
                rts

handleRow6      ldy pauseTune
                bne skipKeyCheck

                cmp #$fe
                beq plusKey
                cmp #$f7
                beq minKey
                cmp #$7f
                beq rewindKey
                rts

rewindKey       lda #$00
                sta rewindStep
                jmp nextRewind

minKey          dec currentSong
                lda currentSong
                cmp #$ff
                bne +
                lda maxSong
                jmp setCurrentSong

plusKey         lda currentSong
                inc currentSong
                cmp maxSong
                bcc +
                lda #0
setCurrentSong  sta currentSong
+               jmp selectSubTune

keyHeld         cpx #$07
                bne +
                cmp #$fd            ; <- held: raise the CPU speed the longer it is held
                beq ffHeld
                rts
+               cpx #$05
                bne +
                cmp #$7f            ; , held: rewind again, with a growing step
                beq rewindHeld
+               rts

; CRSR right fast forwards like <-, CRSR left (SHIFT + CRSR) rewinds like ,
cursorKey       ldx #$02            ; 2 = rewind
                lda #$fd            ; left SHIFT
                sta $dc00
                lda $dc01
                bpl +
                lda #$bf            ; right SHIFT
                sta $dc00
                lda $dc01
                and #$10
                beq +
                dex                 ; 1 = forward
+               cpx cursorHeld
                beq cursorDown
                stx cursorHeld
                dex
                bne +
                jmp startForward
+               ldy pauseTune
                bne cursorDone
                jmp rewindKey
cursorDown      dex
                bne +
                sec                 ; ffHeld expects the carry set
                jmp ffHeld
+               jmp rewindHeld
cursorDone      rts

; 1 MHz for the first 1.5 seconds, then one turbo step more every 0.4 seconds
ffHeld          lda holdTicks
                sbc #15             ; carry is set by the compare above
                bcc releaseTurbo
                lsr
                lsr
                cmp #$0f
                bcc +
                lda #$0f
+               ora #$80            ; badlines off
                bne setTurbo

rewindHeld      ldy pauseTune
                bne +
                lda holdTicks
                cmp #10             ; one step per second
                bcc +
nextRewind      lda #$00
                sta holdTicks
                lda rewindStep
                cmp #60
                bcs rewind
                adc #10             ; carry is clear
                sta rewindStep
                bcc rewind
+               rts

; A tune cannot run backwards, so a rewind restarts the sub tune and fast
; forwards it to the target second with the CPU at full speed.
rewind          sec
                lda clock.elapsedSec
                sbc rewindStep
                tax
                lda clock.elapsedSec + 1
                sbc #$00
                bcs +
                lda #$00            ; target before the start: plain restart
                tax
+               stx seekTarget
                sta seekTarget + 1
                ora seekTarget
                sta seeking
                lda currentSong
                jmp setCurrentSong

; $d031 reads $ff unless Turbo Control is set to U64 Turbo Registers
releaseTurbo    lda origTurbo
setTurbo        ldx origTurbo
                inx
                beq +
                sta $d031
+               rts

; held time in tenths of a second from the CIA 2 TOD clock, which keeps
; real time while the CPU speed changes
pollTod         lda $dd08
                cmp lastTenth
                beq +
                sta lastTenth
                inc holdTicks
                bne +
                dec holdTicks
+               rts

initSeek        lda clock.framesPerSec
                sta clockRates
                sta clockRates + 1
                lda $d031
                sta origTurbo
                sta $dd08           ; starts the TOD clock if it is stopped
                rts

.if INCLUDE_RUNSTOP==1
gotoUltimateMenu
                lda $dffd ; Identification register
                cmp #$c9
                bne noUCI

                ; Check if the UCI is busy
                lda $dffc ; Status register
                and #$30  ; State bits
                bne busyUCI ; Temporarily unavailable

                lda #$84  ; Control Target (Target #4 without reply)
                sta $dffd ; Command pipe
                lda #$05  ; Command Freeze
                sta $dffd ; Command pipe
                ; No params
                lda #$01  ; New Command!
                sta $dffc ; Control register

                ; Now wait patiently
-               lda $dffc
                and #$30  ; Status bits
                bne -
                rts

noUCI           inc $d020
                rts
busyUCI         inc $d021
                rts
.fi
                .section data
keyRow          .byte $fe, $fd, $fb, $f7, $ef, $df, $bf, $7f
currentKey      .byte 0, 0, 0, 0, 0, 0, 0, 0
tuneSelect      .byte 0, 0, 2, 2, 4, 4, 6, 6, 8, 8, 0, 0, 0, 0, 0, 0
runStopPressed  .byte 0
holdTicks       .byte 0
cursorHeld      .byte 0
lastTenth       .byte 0
rewindStep      .byte 0
origTurbo       .byte $ff
seeking         .byte 0
seekTarget      .byte 0, 0
clockRates      .byte 0, 0      ; clock.framesPerSec while playing and while fast forwarding
                .send data
