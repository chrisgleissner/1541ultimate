--------------------------------------------------------------------------------
-- Gideon's Logic Architectures - Copyright 2014
-- Entity: mblite_sdram
-- Date:2015-01-02  
-- Author: Gideon     
-- Description: mblite processor with sdram interface - test module
--------------------------------------------------------------------------------
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
    use work.mem_bus_pkg.all;
    use work.io_bus_pkg.all;
    use work.core_pkg.all;    

entity rvlite_wrapper is
generic (
    g_icache        : boolean := true;
    g_tag_i         : std_logic_vector(7 downto 0) := X"20";
    g_tag_d         : std_logic_vector(7 downto 0) := X"21" );
port (
    clock           : in    std_logic;
    reset           : in    std_logic;
    
    irq_i           : in    std_logic := '0';
    
    io_req          : out   t_io_req;
    io_resp         : in    t_io_resp;
    io_busy         : out   std_logic;
    
    mem_req         : out   t_mem_req_32;
    mem_resp        : in    t_mem_resp_32 );

end entity;

architecture arch of rvlite_wrapper is

    signal dmem_o      : dmem_out_type;
    signal dmem_i      : dmem_in_type;
    signal imem_o      : dmem_out_type;
    signal imem_i      : dmem_in_type;
    signal cimem_o     : imem_out_type;
    signal cimem_i     : imem_in_type;

    signal dmem_req    : t_mem_req_32;
    signal dmem_resp   : t_mem_resp_32;
    signal imem_req    : t_mem_req_32;
    signal imem_resp   : t_mem_resp_32;
begin

    i_core: entity work.core
    port map (
        imem_o => cimem_o,
        imem_i => cimem_i,
        dmem_o => dmem_o,
        dmem_i => dmem_i,
        int_i  => irq_i,
        rst_i  => reset,
        clk_i  => clock
    );

    r_icache: if g_icache generate
        i_cache: entity work.icache
        generic map (
            g_mem_direct     => true )
        port map (
            clock  => clock,
            reset  => reset,
    
            disable => '0',
            
            dmem_i.adr_o => cimem_o.adr_o,
            dmem_i.ena_o => cimem_o.ena_o,
            dmem_i.sel_o => "0000",
            dmem_i.we_o  => '0',
            dmem_i.dat_o => (others => '0'),
            
            dmem_o.ena_i => cimem_i.ena_i,
            dmem_o.dat_i => cimem_i.dat_i,
    
            mem_o  => imem_o,
            mem_i  => imem_i );
        end generate;
      
    r_no_icache: if not g_icache generate
        imem_o.adr_o <= cimem_o.adr_o;
        imem_o.ena_o <= cimem_o.ena_o;
        imem_o.sel_o <= X"F";
        imem_o.we_o  <= '0';
        imem_o.dat_o <= (others => '0');
        
        cimem_i.dat_i <= imem_i.dat_i;
        cimem_i.ena_i <= imem_i.ena_i;
    end generate;

    i_imem: entity work.bus_converter
    generic map (
        g_tag        => g_tag_i,
        g_support_io => false )
    port map (
        clock        => clock,
        reset        => reset,
        dmem_i       => imem_i,
        dmem_o       => imem_o,
        mem_req      => imem_req,
        mem_resp     => imem_resp,
        io_req       => open,
        io_resp      => c_io_resp_init );
    
    i_dmem: entity work.bus_converter
    generic map (
        g_tag        => g_tag_d,
        g_io_bit     => 28,
        g_support_io => true )
    port map (
        clock        => clock,
        reset        => reset,
        dmem_i       => dmem_i,
        dmem_o       => dmem_o,
        mem_req      => dmem_req,
        mem_resp     => dmem_resp,
        io_busy      => io_busy,
        io_req       => io_req,
        io_resp      => io_resp );

    i_arb: entity work.mem_bus_arbiter_pri_32
    generic map (
        g_registered => false,
        g_ports      => 2 )
    port map (
        clock        => clock,
        reset        => reset,
        reqs(0)      => dmem_req,
        reqs(1)      => imem_req,
        resps(0)     => dmem_resp,
        resps(1)     => imem_resp,
        req          => mem_req,
        resp         => mem_resp );
    
end architecture;
